"""Tkinter GUI wrapping all Gooroo Registry Manager operations.

Launch with:  gooroo-registry-gui
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from typing import Optional

from datetime import datetime, timezone
from .checksum import compute_file_checksum, compute_registry_checksum
from .publisher import PublishPlan, S3Publisher
from .registry import CompatibilityRegistryManager
from .validators import Severity, validate_all, validate_schema

# Try to import keyring for secure password storage
try:
    import keyring
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False

# Settings file (persists between launches)
SETTINGS_FILE = Path.home() / ".gooroo-registry-gui" / "settings.json"
KEYRING_SERVICE = "gooroo-registry"


# ---------------------------------------------------------------------------
# Log redirect: forwards print() calls from worker threads to the GUI queue
# ---------------------------------------------------------------------------

class _LogRedirect:
    def __init__(self, q: queue.Queue):
        self._q = q

    def write(self, s: str) -> int:
        if s:
            self._q.put(s)
        return len(s)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Publish confirmation dialog with per-file checkboxes
# ---------------------------------------------------------------------------

class _PublishConfirmDialog(tk.Toplevel):
    """Modal dialog listing files-to-upload with checkboxes.

    After the dialog closes, inspect ``self.result``:
      - ``None``  → user cancelled
      - ``list``  → subset of ``to_upload`` entries that were checked
    """

    def __init__(
        self,
        parent: tk.Tk,
        to_upload: list[tuple],
        already_remote: list[str],
        missing: list[str],
        appcast_overwrites_remote: bool,
        dry_run: bool,
    ):
        super().__init__(parent)
        self.title(("[dry-run] " if dry_run else "") + "Confirm publish")
        self.resizable(True, True)
        self.grab_set()  # modal
        self.result: list[tuple] | None = None

        self._to_upload = to_upload
        self._vars: list[tk.BooleanVar] = []

        P = 8
        # ── Header ──────────────────────────────────────────────────────
        hdr = ("[dry-run] " if dry_run else "") + "Select files to upload:"
        tk.Label(self, text=hdr, font=("TkDefaultFont", 11, "bold"), anchor="w").pack(
            fill="x", padx=P, pady=(P, 2)
        )

        # ── Scrollable checkbox list ────────────────────────────────────
        frame_outer = tk.Frame(self, relief="sunken", bd=1)
        frame_outer.pack(fill="both", expand=True, padx=P, pady=2)

        canvas = tk.Canvas(frame_outer, highlightthickness=0)
        sb = ttk.Scrollbar(frame_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win_id, width=canvas.winfo_width())

        def _on_canvas_configure(event):
            canvas.itemconfigure(win_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        for local_path, remote_path, label in to_upload:
            var = tk.BooleanVar(value=True)
            self._vars.append(var)
            row = tk.Frame(inner)
            row.pack(fill="x", padx=4, pady=1)
            cb = tk.Checkbutton(row, variable=var, anchor="w")
            cb.pack(side="left")
            name_lbl = tk.Label(row, text=label, anchor="w", font=("TkDefaultFont", 10, "bold"))
            name_lbl.pack(side="left")
            path_lbl = tk.Label(
                row,
                text=f"  →  {remote_path}",
                anchor="w",
                foreground="gray40",
                font=("TkDefaultFont", 9),
            )
            path_lbl.pack(side="left")

        # Limit canvas height to ~10 rows, then scroll
        row_h = 24
        canvas_h = min(len(to_upload), 10) * row_h + 8
        canvas.configure(height=canvas_h)

        # ── Select all / none ───────────────────────────────────────────
        sel_frame = tk.Frame(self)
        sel_frame.pack(fill="x", padx=P, pady=(2, 0))
        tk.Button(sel_frame, text="Select all",  command=self._select_all).pack(side="left", padx=2)
        tk.Button(sel_frame, text="Select none", command=self._select_none).pack(side="left", padx=2)

        # ── Info rows ───────────────────────────────────────────────────
        info_frame = tk.Frame(self)
        info_frame.pack(fill="x", padx=P, pady=(4, 0))
        if already_remote:
            tk.Label(
                info_frame,
                text=f"⊘  {len(already_remote)} already on S3 — will be skipped",
                anchor="w", foreground="gray40",
            ).pack(fill="x")
        if missing:
            tk.Label(
                info_frame,
                text=f"✗  {len(missing)} missing (neither local nor remote — see log)",
                anchor="w", foreground="#cc4400",
            ).pack(fill="x")
        if appcast_overwrites_remote:
            tk.Label(
                info_frame,
                text="⚠  appcast.xml already exists on S3 and will be overwritten",
                anchor="w", foreground="#996600",
            ).pack(fill="x")
        if dry_run:
            tk.Label(
                info_frame,
                text="(dry-run — nothing will actually be uploaded)",
                anchor="w", foreground="steelblue",
            ).pack(fill="x")

        # ── Buttons ─────────────────────────────────────────────────────
        btn_frame = tk.Frame(self)
        btn_frame.pack(fill="x", padx=P, pady=P)
        tk.Button(btn_frame, text="Cancel",  width=10, command=self._cancel).pack(side="right", padx=2)
        tk.Button(btn_frame, text="Proceed", width=10, command=self._proceed, default="active").pack(side="right", padx=2)
        self.bind("<Return>", lambda _e: self._proceed())
        self.bind("<Escape>", lambda _e: self._cancel())

        self.update_idletasks()
        # Centre over parent
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        dw = max(self.winfo_reqwidth(), 560)
        dh = self.winfo_reqheight()
        self.geometry(f"{dw}x{dh}+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")

        self.wait_window(self)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _select_all(self):
        for v in self._vars:
            v.set(True)

    def _select_none(self):
        for v in self._vars:
            v.set(False)

    def _proceed(self):
        self.result = [
            entry for entry, var in zip(self._to_upload, self._vars) if var.get()
        ]
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class GoorooRegistryGUI:
    _PAD = 8

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Gooroo Registry Manager")
        self.root.geometry("980x760")
        self.root.minsize(880, 640)

        self._rm: Optional[CompatibilityRegistryManager] = None
        self._log_queue: queue.Queue = queue.Queue()
        self._busy = False
        self._settings = {}

        self._build_ui()
        self._poll_log()
        self._load_settings()
        self._try_auto_load()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        P = self._PAD

        # ── Header: registry path & options ──
        header = ttk.Frame(self.root, padding=P)
        header.pack(fill="x", side="top")
        
        row1 = ttk.Frame(header)
        row1.pack(fill="x")
        ttk.Label(row1, text="Registry:").pack(side="left")
        self._registry_path_var = tk.StringVar(value="registry/compatibility_registry.json")
        ttk.Entry(row1, textvariable=self._registry_path_var, width=54).pack(side="left", padx=(4, 4))
        ttk.Button(row1, text="Browse…", command=self._browse_registry).pack(side="left")
        ttk.Button(row1, text="Load", command=self._load_registry).pack(side="left", padx=(4, 0))
        
        row2 = ttk.Frame(header)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="? Options", command=self._show_all_options_help).pack(side="left", padx=(0, 0))
        
        # Schema version displays and controls
        ttk.Label(row2, text="schemaVersion:").pack(side="left", padx=(24, 4))
        self._header_schema_version_var = tk.StringVar(value="—")
        ttk.Label(row2, textvariable=self._header_schema_version_var, font=("", 11, "bold")).pack(side="left")
        
        ttk.Button(row2, text="+ Increment", command=self._cmd_increment_version).pack(side="left", padx=(8, 0))
        
        self._published_var = tk.BooleanVar(value=False)
        self._synced_var = tk.BooleanVar(value=False)
        
        cb_sync = ttk.Checkbutton(row2, text="Synced", variable=self._synced_var)
        cb_sync.pack(side="left", padx=(24, 4))
        cb_sync.state(['disabled'])

        cb_pub = ttk.Checkbutton(row2, text="Published", variable=self._published_var)
        cb_pub.pack(side="left")
        cb_pub.state(['disabled'])

        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        # ── Notebook tabs ──
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=P, pady=(P, 0))
        self._notebook = nb

        self._tab_status   = ttk.Frame(nb, padding=P)
        self._tab_firmware = ttk.Frame(nb, padding=P)
        self._tab_app      = ttk.Frame(nb, padding=P)
        self._tab_script_ableton = ttk.Frame(nb, padding=P)
        self._tab_script_reaper  = ttk.Frame(nb, padding=P)
        self._tab_pairs    = ttk.Frame(nb, padding=P)
        self._tab_code     = ttk.Frame(nb, padding=P)
        self._tab_sync     = ttk.Frame(nb, padding=P)
        self._tab_publish  = ttk.Frame(nb, padding=P)

        nb.add(self._tab_status,   text="  Status & Validate  ")
        nb.add(self._tab_firmware, text="  Add Firmware  ")
        nb.add(self._tab_app,      text="  Add App  ")
        nb.add(self._tab_script_ableton, text="  Add Ableton Script  ")
        nb.add(self._tab_script_reaper,  text="  Add Reaper Script  ")
        nb.add(self._tab_pairs,    text="  Pairs  ")
        nb.add(self._tab_code,     text="  Code Editor  ")
        nb.add(self._tab_sync,     text="  Sync  ")
        nb.add(self._tab_publish,  text="  Publish  ")

        self._build_status_tab()
        self._build_firmware_tab()
        self._build_app_tab()
        self._build_script_ableton_tab()
        self._build_script_reaper_tab()
        self._build_pairs_tab()
        self._build_code_tab()
        self._build_publish_tab()
        self._build_sync_tab()
        
        # Hook notebook tab change to validate JSON
        nb.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        # ── Log panel ──
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=(P, 0))
        log_frame = ttk.LabelFrame(self.root, text="Output", padding=4)
        log_frame.pack(fill="both", expand=False, padx=P, pady=(4, P))

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=9, state="disabled",
            font=("Menlo", 11), wrap="word",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        self._log_text.pack(fill="both", expand=True)
        self._log_text.tag_configure("error",   foreground="#f48771")
        self._log_text.tag_configure("warning", foreground="#cca700")
        self._log_text.tag_configure("success", foreground="#89d185")

        ttk.Button(log_frame, text="Clear", command=self._clear_log).pack(side="right", pady=(4, 0))

    # ── Tab: Code Editor ─────────────────────────────────────────────────

    def _build_code_tab(self) -> None:
        tab = self._tab_code
        P = self._PAD

        # Title and Save button
        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=(0, P))
        ttk.Label(btn_frame, text="Edit JSON code (validated when switching tabs):", font=("", 11)).pack(side="left")
        ttk.Button(btn_frame, text="Save",            command=self._cmd_save_code).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frame, text="Update Checksum", command=self._cmd_update_editor_checksum).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frame, text="Verify JSON",     command=self._cmd_verify_editor_json).pack(side="right", padx=(4, 0))

        # ScrolledText widget for JSON editing
        text_frame = ttk.Frame(tab)
        text_frame.pack(fill="both", expand=True)
        
        self._code_text = scrolledtext.ScrolledText(
            text_frame, height=20,
            font=("Menlo", 11), wrap="word",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        self._code_text.pack(fill="both", expand=True)
        
        # Info label at bottom
        self._code_status_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self._code_status_var, font=("", 9)).pack(anchor="w", pady=(P, 0))

    # ── Tab: Status & Validate ───────────────────────────────────────────

    def _build_status_tab(self) -> None:
        tab = self._tab_status
        P = self._PAD

        btn_row = ttk.Frame(tab)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Refresh Status", command=self._cmd_status).pack(side="left", padx=(0, 8))

        self._val_strict_var   = tk.BooleanVar(value=False)
        self._val_skip_art_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(btn_row, text="--strict",           variable=self._val_strict_var).pack(side="left")
        ttk.Checkbutton(btn_row, text="--skip-artifacts",   variable=self._val_skip_art_var).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Validate", command=self._cmd_validate).pack(side="left", padx=(8, 0))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)

        info = ttk.Frame(tab)
        info.pack(fill="x")
        labels = ["schemaVersion", "generatedAt", "checksum", "App versions", "Firmware versions", "Axes"]
        self._status_vars: dict[str, tk.StringVar] = {}
        for i, key in enumerate(labels):
            ttk.Label(info, text=f"{key}:", font=("", 11, "bold")).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 12))
            var = tk.StringVar(value="—")
            self._status_vars[key] = var
            ttk.Label(info, textvariable=var, font=("Menlo", 11)).grid(row=i, column=1, sticky="w", pady=2)

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="Validation issues:").pack(anchor="w")

        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, pady=(4, 0))
        self._issues_tree = ttk.Treeview(
            tree_frame, columns=("severity", "rule", "message"), show="headings", height=7,
        )
        self._issues_tree.heading("severity", text="Severity")
        self._issues_tree.heading("rule",     text="Rule")
        self._issues_tree.heading("message",  text="Message")
        self._issues_tree.column("severity", width=80,  anchor="center")
        self._issues_tree.column("rule",     width=220)
        self._issues_tree.column("message",  width=540)
        self._issues_tree.tag_configure("error",   foreground="#cc3300")
        self._issues_tree.tag_configure("warning", foreground="#cc7700")
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._issues_tree.yview)
        self._issues_tree.configure(yscrollcommand=ysb.set)
        self._issues_tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")

    # ── Tab: Artifact Folders ───────────────────────────────────────────────

    def _build_firmware_tab(self) -> None:
        tab = self._tab_firmware
        P = self._PAD
        f = ttk.Frame(tab); f.pack(fill="x")

        self._fw_version_var = tk.StringVar()
        ttk.Label(f, text="Version:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        e_fw_version = ttk.Entry(f, textvariable=self._fw_version_var, width=22)
        e_fw_version.grid(row=0, column=1, sticky="w")
        e_fw_version.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f, text="Add Firmware", command=self._cmd_add_firmware).grid(row=0, column=2, padx=(8, 0))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        # Section 1: Specific firmware file for Adding
        ttk.Label(tab, text="Firmware .lbf file (for adding to registry):", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_file = ttk.Frame(tab); f_file.pack(fill="x", pady=(0, P))
        self._fw_file_var = tk.StringVar()
        e_fw_file = ttk.Entry(f_file, textvariable=self._fw_file_var, width=54)
        e_fw_file.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_fw_file.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_file, text="Browse…", command=self._browse_firmware_file).pack(side="left")

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        # Section 2: General firmware folder for Publishing
        ttk.Label(tab, text="Firmware folder path (for publishing bulk artifacts):", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_folder = ttk.Frame(tab); f_folder.pack(fill="x", pady=(0, P))
        self._fw_folder_var = tk.StringVar()
        e_fw_folder = ttk.Entry(f_folder, textvariable=self._fw_folder_var, width=54)
        e_fw_folder.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_fw_folder.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_folder, text="Browse…", command=self._browse_firmware_folder).pack(side="left")
        ttk.Label(tab, text="(local folder containing all .lbf firmware files)", foreground="gray").pack(anchor="w", pady=(0, P))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        ttk.Label(tab, text="S3 path for firmware (defaults to /firmware/Liobox2/filename):", font=("", 11)).pack(anchor="w", pady=(0, P))
        self._fw_s3_path_var = tk.StringVar(value="firmware/Liobox2/")
        e_fw_s3 = ttk.Entry(tab, textvariable=self._fw_s3_path_var, width=54)
        e_fw_s3.pack(fill="x", pady=(0, P))
        e_fw_s3.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(tab, text="(e.g., firmware/Liobox2/)", foreground="gray").pack(anchor="w", pady=(0, P))

    # ── Tab: Add App Version ─────────────────────────────────────────────

    def _build_app_tab(self) -> None:
        tab = self._tab_app
        P = self._PAD
        f = ttk.Frame(tab); f.pack(fill="x")

        self._app_version_var  = tk.StringVar()
        self._app_gprot_var    = tk.StringVar()
        self._app_datamodel_var= tk.StringVar()
        self._app_std_var      = tk.StringVar()
        self._app_dev_var      = tk.StringVar()

        # App version row with button
        ttk.Label(f, text="App version:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        e_app_version = ttk.Entry(f, textvariable=self._app_version_var, width=22)
        e_app_version.grid(row=0, column=1, sticky="w")
        e_app_version.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f, text="Add App Version", command=self._cmd_add_app).grid(row=0, column=2, padx=(8, 0))

        # Protocol fields
        rows = [
            ("gprotocol_version:",     self._app_gprot_var),
            ("device_datamodel:",      self._app_datamodel_var),
            ("std_command_set:",       self._app_std_var),
            ("dev_command_set:",       self._app_dev_var),
        ]
        for i, (lbl, var) in enumerate(rows, start=1):
            ttk.Label(f, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 8))
            e = ttk.Entry(f, textvariable=var, width=22)
            e.grid(row=i, column=1, sticky="w")
            e.bind("<FocusOut>", lambda e: self._save_settings())
        
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="App folder path:", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_app = ttk.Frame(tab); f_app.pack(fill="x", pady=(0, P))
        self._app_folder_var = tk.StringVar()
        e_app_folder = ttk.Entry(f_app, textvariable=self._app_folder_var, width=54)
        e_app_folder.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_app_folder.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_app, text="Browse…", command=self._browse_app_folder).pack(side="left")
        ttk.Label(tab, text="(folder containing appcast.xml and .zip files)", foreground="gray").pack(anchor="w", pady=(0, P))
        
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="S3 path for appcast.xml:", font=("", 11)).pack(anchor="w", pady=(0, P))
        self._app_appcast_s3_path_var = tk.StringVar(value="software/GoorooLink/production/appcast.xml")
        e_app_appcast_s3 = ttk.Entry(tab, textvariable=self._app_appcast_s3_path_var, width=54)
        e_app_appcast_s3.pack(fill="x", pady=(0, P))
        e_app_appcast_s3.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(tab, text="(e.g., software/GoorooLink/production/appcast.xml)", foreground="gray").pack(anchor="w", pady=(0, P))
        
        ttk.Label(tab, text="S3 path for app versions:", font=("", 11)).pack(anchor="w", pady=(0, P))
        self._app_versions_s3_path_var = tk.StringVar(value="software/GoorooLink/production/")
        e_app_versions_s3 = ttk.Entry(tab, textvariable=self._app_versions_s3_path_var, width=54)
        e_app_versions_s3.pack(fill="x", pady=(0, P))
        e_app_versions_s3.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(tab, text="(e.g., software/GoorooLink/production/ - version zips will be uploaded here)", foreground="gray").pack(anchor="w", pady=(0, P))

    # ── Tab: Add Ableton Script ─────────────────────────────────────────
    # (Removed - now using Scripts folder from App tab)

    def _build_script_ableton_tab(self) -> None:
        tab = self._tab_script_ableton
        P = self._PAD
        f = ttk.Frame(tab); f.pack(fill="x")

        self._script_ableton_version_var = tk.StringVar()
        ttk.Label(f, text="Version:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        e_ableton_version = ttk.Entry(f, textvariable=self._script_ableton_version_var, width=22)
        e_ableton_version.grid(row=0, column=1, sticky="w")
        e_ableton_version.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f, text="Add Ableton Script", command=self._cmd_add_script_ableton).grid(row=0, column=2, padx=(8, 0))
        
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        # Section 1: Specific script file for Adding
        ttk.Label(tab, text="Ableton Script .zip file (for adding to registry):", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_file = ttk.Frame(tab); f_file.pack(fill="x", pady=(0, P))
        self._script_ableton_file_var = tk.StringVar()
        e_file = ttk.Entry(f_file, textvariable=self._script_ableton_file_var, width=54)
        e_file.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_file.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_file, text="Browse…", command=self._browse_ableton_script_file).pack(side="left")

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        # Section 2: General scripts folder for Publishing
        ttk.Label(tab, text="Scripts folder path (for publishing bulk artifacts):", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_scripts = ttk.Frame(tab); f_scripts.pack(fill="x", pady=(0, P))
        self._ableton_scripts_folder_var = tk.StringVar()
        e_scripts = ttk.Entry(f_scripts, textvariable=self._ableton_scripts_folder_var, width=54)
        e_scripts.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_scripts.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_scripts, text="Browse…", command=self._browse_ableton_scripts_folder).pack(side="left")
        ttk.Label(tab, text="(local folder containing ableton/ subdirectory with .zip files)", foreground="gray").pack(anchor="w", pady=(0, P))
        
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="S3 path for ableton scripts (defaults to /scripts/Ableton/filename):", font=("", 11)).pack(anchor="w", pady=(0, P))
        self._ableton_s3_path_var = tk.StringVar(value="scripts/Ableton/")
        e_ableton_s3 = ttk.Entry(tab, textvariable=self._ableton_s3_path_var, width=54)
        e_ableton_s3.pack(fill="x", pady=(0, P))
        e_ableton_s3.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(tab, text="(e.g., scripts/Ableton/)", foreground="gray").pack(anchor="w", pady=(0, P))

    # ── Tab: Add Reaper Script ──────────────────────────────────────────
    # (Removed - now using Scripts folder from App tab)

    def _build_script_reaper_tab(self) -> None:
        tab = self._tab_script_reaper
        P = self._PAD
        f = ttk.Frame(tab); f.pack(fill="x")

        self._script_reaper_version_var = tk.StringVar()
        ttk.Label(f, text="Version:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        e_reaper_version = ttk.Entry(f, textvariable=self._script_reaper_version_var, width=22)
        e_reaper_version.grid(row=0, column=1, sticky="w")
        e_reaper_version.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f, text="Add Reaper Script", command=self._cmd_add_script_reaper).grid(row=0, column=2, padx=(8, 0))
        
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        # Section 1: Specific script file for Adding
        ttk.Label(tab, text="Reaper Script .zip file (for adding to registry):", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_file = ttk.Frame(tab); f_file.pack(fill="x", pady=(0, P))
        self._script_reaper_file_var = tk.StringVar()
        e_file = ttk.Entry(f_file, textvariable=self._script_reaper_file_var, width=54)
        e_file.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_file.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_file, text="Browse…", command=self._browse_reaper_script_file).pack(side="left")

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        # Section 2: General scripts folder for Publishing
        ttk.Label(tab, text="Scripts folder path (for publishing bulk artifacts):", font=("", 11)).pack(anchor="w", pady=(0, P))
        f_scripts = ttk.Frame(tab); f_scripts.pack(fill="x", pady=(0, P))
        self._reaper_scripts_folder_var = tk.StringVar()
        e_scripts = ttk.Entry(f_scripts, textvariable=self._reaper_scripts_folder_var, width=54)
        e_scripts.pack(side="left", fill="x", expand=True, padx=(0, 4))
        e_scripts.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f_scripts, text="Browse…", command=self._browse_reaper_scripts_folder).pack(side="left")
        ttk.Label(tab, text="(local folder containing reaper/ subdirectory with .zip files)", foreground="gray").pack(anchor="w", pady=(0, P))
        
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="S3 path for reaper scripts (defaults to /scripts/Reaper/filename):", font=("", 11)).pack(anchor="w", pady=(0, P))
        self._reaper_s3_path_var = tk.StringVar(value="scripts/Reaper/")
        e_reaper_s3 = ttk.Entry(tab, textvariable=self._reaper_s3_path_var, width=54)
        e_reaper_s3.pack(fill="x", pady=(0, P))
        e_reaper_s3.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(tab, text="(e.g., scripts/Reaper/)", foreground="gray").pack(anchor="w", pady=(0, P))

    # ── Tab: Pairs ───────────────────────────────────────────────────────

    def _build_pairs_tab(self) -> None:
        tab = self._tab_pairs
        P = self._PAD
        f = ttk.Frame(tab); f.pack(fill="x")

        self._pair_axis_var  = tk.StringVar(value="app_firmware")
        self._pair_left_var  = tk.StringVar()
        self._pair_right_var = tk.StringVar()

        ttk.Label(f, text="Axis:").grid(         row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        self._pair_axis_combo = ttk.Combobox(f, textvariable=self._pair_axis_var, width=32)
        self._pair_axis_combo.grid(row=0, column=1, sticky="w")

        ttk.Label(f, text="Left version:").grid( row=1, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(f, textvariable=self._pair_left_var, width=22).grid(row=1, column=1, sticky="w")

        ttk.Label(f, text="Right version:").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(f, textvariable=self._pair_right_var, width=22).grid(row=2, column=1, sticky="w")

        btn_row = ttk.Frame(tab); btn_row.pack(anchor="w", pady=(P, 0))
        ttk.Button(btn_row, text="Add Pair",    command=self._cmd_add_pair).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Remove Pair", command=self._cmd_remove_pair).pack(side="left")

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="All current pairs (click a row to populate the form):").pack(anchor="w")

        tree_frame = ttk.Frame(tab); tree_frame.pack(fill="both", expand=True, pady=(4, 0))
        self._pairs_tree = ttk.Treeview(
            tree_frame, columns=("axis", "left", "right"), show="headings", height=10,
        )
        self._pairs_tree.heading("axis",  text="Axis")
        self._pairs_tree.heading("left",  text="Left Version")
        self._pairs_tree.heading("right", text="Right Version")
        self._pairs_tree.column("axis",  width=300)
        self._pairs_tree.column("left",  width=160)
        self._pairs_tree.column("right", width=160)
        # Configure alternating row colors with proper contrast
        self._pairs_tree.tag_configure("oddrow", background="#ffffff", foreground="#000000")
        self._pairs_tree.tag_configure("evenrow", background="#e8f0f5", foreground="#000000")
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._pairs_tree.yview)
        self._pairs_tree.configure(yscrollcommand=ysb.set)
        self._pairs_tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self._pairs_tree.bind("<<TreeviewSelect>>", self._on_pair_select)

    # ── Credentials helper ───────────────────────────────────────────────

    def _load_openstack_credentials(self) -> None:
        """Load OpenStack credentials: .sh file + keychain password."""
        # Step 1: Load .sh file (for username, project_name, auth_url)
        path = filedialog.askopenfilename(
            title="Select OpenStack credentials file (openstack_env*.sh)",
            filetypes=[("Shell scripts", "*.sh"), ("All files", "*.*")],
        )
        if not path:
            return
        
        filepath = Path(path)
        if not filepath.exists():
            messagebox.showerror("Error", f"File not found: {path}")
            return
        
        try:
            # Parse the .sh file for export statements
            with open(filepath, "r") as f:
                content = f.read()
            
            # Extract export KEY=VALUE lines (including quoted values)
            pattern = r'export\s+(\w+)\s*=\s*["\']?([^"\'\n]*)["\']?'
            matches = re.findall(pattern, content)
            
            if not matches:
                messagebox.showwarning("No variables found", f"Could not find any export statements in {filepath.name}")
                return
            
            # Load ALL exported vars from .sh file into environment
            sh_creds = {key: value for key, value in matches if value}
            username = sh_creds.get("OS_USERNAME")
            project_name = sh_creds.get("OS_PROJECT_NAME")
            auth_url = sh_creds.get("OS_AUTH_URL")
            
            if not all([username, project_name, auth_url]):
                missing = []
                if not username: missing.append("OS_USERNAME")
                if not project_name: missing.append("OS_PROJECT_NAME")
                if not auth_url: missing.append("OS_AUTH_URL")
                messagebox.showerror(
                    "Missing credentials in .sh file",
                    f"Required fields not found:\n  " + "\n  ".join(missing)
                )
                return
            
            # Load all exported vars into the process environment
            for key, value in sh_creds.items():
                os.environ[key] = value
            
            # Step 2: Load password from keychain
            if HAS_KEYRING:
                # Pre-fill with previously saved values if available
                saved_service = self._settings.get("keychain_service", "")
                saved_account = self._settings.get("keychain_account", "")

                keychain_name = tk.simpledialog.askstring(
                    "Keychain Service",
                    "Enter the keychain service name:\n(e.g., Infomaniak_OpenStack)",
                    initialvalue=saved_service,
                )
                if not keychain_name:
                    return

                keychain_account = tk.simpledialog.askstring(
                    "Keychain Account",
                    "Enter the account name in the keychain:\n(e.g., PCU-RULNDDL)",
                    initialvalue=saved_account,
                )
                if not keychain_account:
                    return

                try:
                    password = keyring.get_password(keychain_name, keychain_account)
                    if password:
                        os.environ["OS_PASSWORD"] = password
                        # Persist everything needed for silent auto-restore on next launch
                        self._settings["os_sh_file"]       = str(filepath)
                        self._settings["os_username"]      = username
                        self._settings["os_project_name"]  = project_name
                        self._settings["os_auth_url"]      = auth_url
                        self._settings["keychain_service"] = keychain_name
                        self._settings["keychain_account"] = keychain_account
                        self._save_settings()
                        self._update_creds_status(filepath.name, keychain_name, keychain_account)
                        
                        messagebox.showinfo(
                            "Credentials loaded",
                            f"Loaded from {filepath.name} + keychain '{keychain_name}' (account '{keychain_account}'):\n"
                            f"  OS_USERNAME = {username}\n"
                            f"  OS_PROJECT_NAME = {project_name}\n"
                            f"  OS_AUTH_URL = {auth_url}\n"
                            f"  OS_PASSWORD = ••••••••"
                        )
                    else:
                        messagebox.showwarning(
                            "Password not in keychain",
                            f"Password not found in keychain service '{keychain_name}', account '{keychain_account}'."
                        )
                except Exception as e:
                    messagebox.showerror("Error loading from keychain", f"Failed:\n{str(e)}")
            else:
                messagebox.showinfo(
                    "Credentials loaded (partial)",
                    f"Loaded from {filepath.name}:\n"
                    f"  OS_USERNAME = {username}\n"
                    f"  OS_PROJECT_NAME = {project_name}\n"
                    f"  OS_AUTH_URL = {auth_url}\n\n"
                    f"Keyring not available. Please set OS_PASSWORD manually or install keyring."
                )
        except Exception as e:
            messagebox.showerror("Error loading credentials", f"Failed to parse file:\n{str(e)}")



    # ── Tab: Publish ─────────────────────────────────────────────────────

    def _build_publish_tab(self) -> None:
        tab = self._tab_publish
        P = self._PAD

        ttk.Label(tab, text="Upload artifacts and the registry to Infomaniak S3.", font=("", 11)).pack(anchor="w", pady=(0, P))

        self._pub_dry_run_var     = tk.BooleanVar(value=True)
        self._pub_strict_var      = tk.BooleanVar(value=False)
        self._pub_skip_val_var    = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="--dry-run  (preview only — nothing will be uploaded)", variable=self._pub_dry_run_var).pack(anchor="w")
        ttk.Checkbutton(tab, text="--strict   (refuse if there are validation warnings)", variable=self._pub_strict_var).pack(anchor="w", pady=4)
        ttk.Checkbutton(tab, text="--skip-validate  (not recommended)",                   variable=self._pub_skip_val_var).pack(anchor="w")

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        
        creds_frame = ttk.Frame(tab)
        creds_frame.pack(fill="x")
        ttk.Label(creds_frame, text="OpenStack Credentials:", font=("", 10)).pack(anchor="w", pady=(0, 4))
        
        file_btn_frame = ttk.Frame(creds_frame)
        file_btn_frame.pack(anchor="w", padx=(20, 0), pady=(0, 4))
        ttk.Button(file_btn_frame, text="Load from .sh file…", command=self._load_openstack_credentials).pack(side="left", padx=(0, 4))
        self._creds_status_var = tk.StringVar(value="")
        self._creds_status_label = ttk.Label(file_btn_frame, textvariable=self._creds_status_var, font=("", 9))
        self._creds_status_label.pack(side="left", padx=(4, 0))
        ttk.Label(file_btn_frame, text="  (username, project, auth URL from .sh + password from keychain)", foreground="gray", font=("", 9)).pack(side="left")
        
        ttk.Label(creds_frame, text="(or source config/openstack_env.sh in terminal before launching)", foreground="gray", font=("", 9)).pack(anchor="w", padx=(20, 0))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)
        ttk.Label(tab, text="S3 path for registry:", font=("", 11)).pack(anchor="w", pady=(0, P))
        self._registry_s3_path_var = tk.StringVar(value="software/GoorooLink/production/compatibility_registry.json")
        e_registry_s3 = ttk.Entry(tab, textvariable=self._registry_s3_path_var, width=54)
        e_registry_s3.pack(fill="x", pady=(0, P))
        e_registry_s3.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(tab, text="(e.g., software/GoorooLink/production/compatibility_registry.json)", foreground="gray").pack(anchor="w", pady=(0, P))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=P)

        btn_row = ttk.Frame(tab)
        btn_row.pack(anchor="w", pady=(P, 0))
        ttk.Button(btn_row, text="List Remote", command=self._cmd_list_remote).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Publish", command=self._cmd_publish).pack(side="left")

    # ── Tab: Sync ────────────────────────────────────────────────────────

    def _build_sync_tab(self) -> None:
        tab = self._tab_sync
        P = self._PAD

        ttk.Label(tab, text="Copy the registry to a GoorooLink assets directory for embedding.", font=("", 11)).pack(anchor="w", pady=(0, P))

        self._sync_target_var = tk.StringVar()
        f = ttk.Frame(tab); f.pack(fill="x")
        ttk.Label(f, text="Target directory:").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        e_sync_target = ttk.Entry(f, textvariable=self._sync_target_var, width=54)
        e_sync_target.grid(row=0, column=1, sticky="w")
        e_sync_target.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Button(f, text="Browse…", command=self._browse_sync_target).grid(row=0, column=2, padx=(4, 0))

        ttk.Button(tab, text="Sync", command=self._cmd_sync).pack(anchor="w", pady=(P, 0))

    # ── Log helpers ──────────────────────────────────────────────────────

    def _log(self, text: str, tag: str = "") -> None:
        self._log_text.config(state="normal")
        self._log_text.insert("end", text, tag if tag else "")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _log_line(self, text: str = "", tag: str = "") -> None:
        self._log(text + "\n", tag)

    def _clear_log(self) -> None:
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                lower = msg.lower()
                if "✗" in msg or "error" in lower or "failed" in lower or "refused" in lower:
                    tag = "error"
                elif "⚠" in msg or "warn" in lower:
                    tag = "warning"
                elif "✓" in msg or "saved" in lower or "complete" in lower or "passed" in lower or "copied" in lower:
                    tag = "success"
                else:
                    tag = ""
                self._log(msg, tag)
        except Exception:
            pass
        self.root.after(100, self._poll_log)

    # ── Registry loading ─────────────────────────────────────────────────

    def _try_auto_load(self) -> None:
        if Path(self._registry_path_var.get()).exists():
            self._load_registry()

    # ── Settings persistence ────────────────────────────────────────────

    def _load_settings(self) -> None:
        """Load saved field values from disk (if file exists)."""
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self._settings = json.load(f)

            # Restore firmware tab
            self._fw_version_var.set(self._settings.get("fw_version", ""))
            self._fw_s3_path_var.set(self._settings.get("fw_s3_path", "firmware/Liobox2/"))

            # Restore app tab
            self._app_version_var.set(self._settings.get("app_version", ""))
            self._app_gprot_var.set(self._settings.get("app_gprot", ""))
            self._app_datamodel_var.set(self._settings.get("app_datamodel", ""))
            self._app_std_var.set(self._settings.get("app_std_cmd", ""))
            self._app_dev_var.set(self._settings.get("app_dev_cmd", ""))
            self._app_appcast_s3_path_var.set(self._settings.get("app_appcast_s3_path", "software/GoorooLink/production/appcast.xml"))
            self._app_versions_s3_path_var.set(self._settings.get("app_versions_s3_path", "software/GoorooLink/production/"))

            # Restore ableton script tab
            self._script_ableton_version_var.set(self._settings.get("script_ableton_version", ""))
            self._ableton_s3_path_var.set(self._settings.get("ableton_s3_path", "scripts/Ableton/"))

            # Restore reaper script tab
            self._script_reaper_version_var.set(self._settings.get("script_reaper_version", ""))
            self._reaper_s3_path_var.set(self._settings.get("reaper_s3_path", "scripts/Reaper/"))

            # Restore sync target
            self._sync_target_var.set(self._settings.get("sync_target", ""))

            # Restore publish tab
            self._registry_s3_path_var.set(self._settings.get("registry_s3_path", "software/GoorooLink/production/compatibility_registry.json"))

            # Restore artifact folders
            self._fw_folder_var.set(self._settings.get("fw_folder", ""))
            self._app_folder_var.set(self._settings.get("app_folder", ""))
            self._ableton_scripts_folder_var.set(self._settings.get("ableton_scripts_folder", ""))
            self._reaper_scripts_folder_var.set(self._settings.get("reaper_scripts_folder", ""))

            # Restore specific files
            self._fw_file_var.set(self._settings.get("fw_file", ""))
            self._script_ableton_file_var.set(self._settings.get("script_ableton_file", ""))
            self._script_reaper_file_var.set(self._settings.get("script_reaper_file", ""))

            # Auto-restore OpenStack credentials if a working combination was saved
            self._try_auto_load_credentials()

        except Exception as e:
            self._log_line(f"⚠ Could not load settings: {e}", "warning")

    def _save_settings(self) -> None:
        """Save field values to disk for next launch."""
        _CRED_KEYS = ("os_sh_file", "keychain_service", "keychain_account",
                      "os_username", "os_project_name", "os_auth_url")
        try:
            # Preserve credential keys that are managed by _load_openstack_credentials
            preserved = {k: self._settings[k] for k in _CRED_KEYS if k in self._settings}
            self._settings = {
                **preserved,
                "fw_version": self._fw_version_var.get(),
                "fw_file": self._fw_file_var.get(),
                "fw_folder": self._fw_folder_var.get(),
                "fw_s3_path": self._fw_s3_path_var.get(),
                "app_version": self._app_version_var.get(),
                "app_gprot": self._app_gprot_var.get(),
                "app_datamodel": self._app_datamodel_var.get(),
                "app_std_cmd": self._app_std_var.get(),
                "app_dev_cmd": self._app_dev_var.get(),
                "app_folder": self._app_folder_var.get(),
                "app_appcast_s3_path": self._app_appcast_s3_path_var.get(),
                "app_versions_s3_path": self._app_versions_s3_path_var.get(),
                "script_ableton_version": self._script_ableton_version_var.get(),
                "script_ableton_file": self._script_ableton_file_var.get(),
                "ableton_scripts_folder": self._ableton_scripts_folder_var.get(),
                "ableton_s3_path": self._ableton_s3_path_var.get(),
                "script_reaper_version": self._script_reaper_version_var.get(),
                "script_reaper_file": self._script_reaper_file_var.get(),
                "reaper_scripts_folder": self._reaper_scripts_folder_var.get(),
                "reaper_s3_path": self._reaper_s3_path_var.get(),
                "sync_target": self._sync_target_var.get(),
                "registry_s3_path": self._registry_s3_path_var.get(),
            }
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
        except Exception as e:
            self._log_line(f"⚠ Could not save settings: {e}", "warning")

    def _try_auto_load_credentials(self) -> None:
        """Silently re-apply saved OpenStack credentials on startup."""
        if not HAS_KEYRING:
            return
        sh_file          = self._settings.get("os_sh_file", "")
        keychain_service = self._settings.get("keychain_service", "")
        keychain_account = self._settings.get("keychain_account", "")
        if not all([sh_file, keychain_service, keychain_account]):
            return
        filepath = Path(sh_file)
        if not filepath.exists():
            return
        try:
            with open(filepath, "r") as f:
                content = f.read()
            sh_creds = {k: v for k, v in re.findall(
                r'export\s+(\w+)\s*=\s*["\']?([^"\'\'\n]*)["\']?', content
            ) if v}
            username     = sh_creds.get("OS_USERNAME")
            project_name = sh_creds.get("OS_PROJECT_NAME")
            auth_url     = sh_creds.get("OS_AUTH_URL")
            if not all([username, project_name, auth_url]):
                return
            password = keyring.get_password(keychain_service, keychain_account)
            if not password:
                return
            # Load all exported vars into the process environment
            for key, value in sh_creds.items():
                os.environ[key] = value
            os.environ["OS_PASSWORD"] = password
            self._log_line(
                f"✓ OpenStack credentials auto-loaded from {filepath.name}"
                f" + keychain '{keychain_service}' / '{keychain_account}'.",
                "success",
            )
            self._update_creds_status(filepath.name, keychain_service, keychain_account)
        except Exception:
            pass  # Silent — user can load manually via the button

    def _update_creds_status(self, sh_name: str, service: str, account: str) -> None:
        """Update the credential status label in the Publish tab."""
        self._creds_status_var.set(f"✓ loaded: {sh_name} + {service}/{account}")
        self._creds_status_label.configure(foreground="#2d7a2d")

    def _browse_registry(self) -> None:
        path = filedialog.askopenfilename(
            title="Select compatibility_registry.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self._registry_path_var.set(path)
            self._load_registry()

    def _load_registry(self) -> None:
        path = Path(self._registry_path_var.get())
        if not path.exists():
            messagebox.showerror("Error", f"Registry not found: {path}")
            return
        try:
            rm = CompatibilityRegistryManager(path)
            rm.load()
            self._rm = rm
            self._log_line(f"Loaded: {path}", "success")
            self._refresh_ui()
        except Exception as exc:
            messagebox.showerror("Load error", str(exc))

    # ── UI refresh after load / mutation ─────────────────────────────────

    def _refresh_ui(self) -> None:
        self._refresh_status_panel()
        self._refresh_pairs_tree()
        self._refresh_axis_combo()
        self._refresh_code_text()

    def _refresh_status_panel(self) -> None:
        if self._rm is None:
            return
        data = self._rm.data
        app_vers = list(data.get("protocol_requirements", {}).keys())
        fw_vers  = list(data.get("firmware", {}).keys())
        axes     = list(data.get("axes", {}).keys())

        checksum = data.get("checksum", "—")
        schema_ver = data.get("schemaVersion", "—")
        self._status_vars["schemaVersion"].set(schema_ver)
        self._header_schema_version_var.set(schema_ver)
        self._status_vars["generatedAt"].set(data.get("generatedAt", "—"))
        self._status_vars["checksum"].set(checksum[:36] + "…" if len(checksum) > 36 else checksum)
        self._status_vars["App versions"].set(f"{len(app_vers)}   ({', '.join(app_vers)})")
        self._status_vars["Firmware versions"].set(f"{len(fw_vers)}   ({', '.join(fw_vers)})")
        self._status_vars["Axes"].set(", ".join(axes))

        # Update published/synced indicators based on saved checksums
        sync_chk = self._settings.get("synced_checksum", "")
        pub_chk = self._settings.get("published_checksum", "")
        self._synced_var.set(checksum == sync_chk and checksum != "—")
        self._published_var.set(checksum == pub_chk and checksum != "—")

    def _refresh_pairs_tree(self) -> None:
        if self._rm is None:
            return
        tree = self._pairs_tree
        tree.delete(*tree.get_children())
        row_index = 0
        for axis_name, axis_data in self._rm.data.get("axes", {}).items():
            for left, right_list in axis_data.get("pairs", {}).items():
                for right in right_list:
                    # Alternate row colors for readability
                    tag = "evenrow" if row_index % 2 == 0 else "oddrow"
                    tree.insert("", "end", values=(axis_name, left, right), tags=(tag,))
                    row_index += 1

    def _refresh_axis_combo(self) -> None:
        if self._rm is None:
            return
        axes = list(self._rm.data.get("axes", {}).keys())
        self._pair_axis_combo["values"] = axes

    def _on_pair_select(self, _event=None) -> None:
        sel = self._pairs_tree.selection()
        if not sel:
            return
        axis, left, right = self._pairs_tree.item(sel[0])["values"]
        self._pair_axis_var.set(axis)
        self._pair_left_var.set(left)
        self._pair_right_var.set(right)

    def _refresh_code_text(self) -> None:
        """Refresh the code editor with current JSON."""
        if self._rm is None:
            self._code_text.config(state="normal")
            self._code_text.delete("1.0", "end")
            self._code_text.config(state="disabled")
            return
        json_str = json.dumps(self._rm.data, indent=4, ensure_ascii=False)
        self._code_text.config(state="normal")
        self._code_text.delete("1.0", "end")
        self._code_text.insert("1.0", json_str)
        self._code_text.config(state="normal")
        self._code_status_var.set("✓ In sync")

    def _on_notebook_tab_changed(self, _event=None) -> None:
        """Validate JSON when leaving the Code tab."""
        if self._notebook.index(self._notebook.select()) == self._notebook.index(self._tab_code):
            # Entering the Code tab — no action needed
            return
        
        # Validate and update tab color
        self._validate_and_color_code_tab()

    def _validate_and_color_code_tab(self) -> bool:
        """Validate JSON in code editor and color the tab red if invalid."""
        code_text = self._code_text.get("1.0", "end").strip()
        
        if not code_text:
            self._code_status_var.set("⚠ Empty")
            self._set_code_tab_color("red")
            return False
        
        try:
            json.loads(code_text)
            self._code_status_var.set("✓ Valid JSON")
            return True
        except json.JSONDecodeError as e:
            self._code_status_var.set(f"✗ Invalid JSON: {e.msg} (line {e.lineno})")
            return False

    # ── Thread runner ────────────────────────────────────────────────────

    def _run_in_thread(self, fn) -> None:
        """Execute fn() in a daemon thread, redicting stdout to the log widget."""
        if self._busy:
            self._log_line("⚠ Another operation is already running.", "warning")
            return
        self._busy = True
        redirect = _LogRedirect(self._log_queue)

        def _wrapper():
            import sys as _sys
            old_out, old_err = _sys.stdout, _sys.stderr
            _sys.stdout = redirect  # type: ignore[assignment]
            _sys.stderr = redirect  # type: ignore[assignment]
            try:
                fn()
            except Exception as exc:
                self._log_queue.put(f"✗ Error: {exc}\n")
            finally:
                _sys.stdout = old_out
                _sys.stderr = old_err
                self._busy = False
                # Reload and refresh on the main thread
                self.root.after(200, self._post_mutation_refresh)

        threading.Thread(target=_wrapper, daemon=True).start()

    def _post_mutation_refresh(self) -> None:
        if self._rm is not None:
            self._rm.load()
            self._refresh_ui()

    # ── File / directory pickers ─────────────────────────────────────────

    def _browse_file(self, var: tk.StringVar, filetypes: list) -> None:
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            var.set(path)

    def _browse_sync_target(self) -> None:
        path = filedialog.askdirectory(title="Select GoorooLink assets directory")
        if path:
            self._sync_target_var.set(path)

    def _browse_firmware_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Firmware (.lbf) file",
            filetypes=[("Liobox Firmware", "*.lbf"), ("All files", "*.*")]
        )
        if path:
            self._fw_file_var.set(path)
            self._save_settings()

    def _browse_firmware_folder(self) -> None:
        path = filedialog.askdirectory(title="Select Firmware folder (containing .lbf files)")
        if path:
            self._fw_folder_var.set(path)
            self._save_settings()

    def _browse_app_folder(self) -> None:
        path = filedialog.askdirectory(title="Select App folder (containing appcast.xml and .zip files)")
        if path:
            self._app_folder_var.set(path)
            self._save_settings()

    def _browse_ableton_script_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Ableton Script (.zip) file",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")]
        )
        if path:
            self._script_ableton_file_var.set(path)
            self._save_settings()

    def _browse_ableton_scripts_folder(self) -> None:
        path = filedialog.askdirectory(title="Select Scripts folder (containing ableton/ subdirectory)")
        if path:
            self._ableton_scripts_folder_var.set(path)
            self._save_settings()

    def _browse_reaper_script_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Reaper Script (.zip) file",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")]
        )
        if path:
            self._script_reaper_file_var.set(path)
            self._save_settings()

    def _browse_reaper_scripts_folder(self) -> None:
        path = filedialog.askdirectory(title="Select Scripts folder (containing reaper/ subdirectory)")
        if path:
            self._reaper_scripts_folder_var.set(path)
            self._save_settings()

    # ── Guard ────────────────────────────────────────────────────────────

    def _require_rm(self) -> bool:
        if self._rm is None:
            messagebox.showerror("No registry loaded", "Load a registry file first.")
            return False
        return True

    # ── Commands ─────────────────────────────────────────────────────────

    def _cmd_status(self) -> None:
        if not self._require_rm():
            return
        self._log_line("─" * 60)
        data = self._rm.data
        app_vers = list(data.get("protocol_requirements", {}).keys())
        fw_vers  = list(data.get("firmware", {}).keys())
        self._log_line(f"schemaVersion : {data.get('schemaVersion')}")
        self._log_line(f"generatedAt   : {data.get('generatedAt')}")
        self._log_line(f"App versions  : {', '.join(app_vers) or '—'}")
        self._log_line(f"Firmware      : {', '.join(fw_vers) or '—'}")
        for axis_name, axis_data in data.get("axes", {}).items():
            total   = sum(len(v) for v in axis_data.get("pairs", {}).values())
            scripts = len(axis_data.get("available_scripts", {}))
            extra   = f", {scripts} script(s)" if scripts else ""
            self._log_line(f"  [{axis_name}]: {total} pair(s){extra}")
        self._refresh_status_panel()

    def _cmd_validate(self) -> None:
        if not self._require_rm():
            return
        self._log_line("─" * 60)
        self._log_line("Validating…")

        strict       = self._val_strict_var.get()
        skip_arts    = self._val_skip_art_var.get()
        artifacts_dir = None if skip_arts else (self._rm.path.parent.parent / "artifacts")

        issues = validate_all(self._rm.data, artifacts_dir=artifacts_dir)
        errors   = [i for i in issues if i.severity == Severity.ERROR]
        warnings = [i for i in issues if i.severity == Severity.WARNING]

        self._issues_tree.delete(*self._issues_tree.get_children())
        for issue in issues:
            tag = "error" if issue.severity == Severity.ERROR else "warning"
            self._issues_tree.insert("", "end",
                values=(issue.severity.value, issue.rule, issue.message), tags=(tag,))
            sym = "✗" if issue.severity == Severity.ERROR else "⚠"
            self._log_line(f"  {sym} [{issue.rule}] {issue.message}", tag)

        if not issues:
            self._log_line("✓ All checks passed.", "success")
        else:
            summary_tag = "error" if errors or (strict and warnings) else "warning"
            self._log_line(f"\n{len(errors)} error(s), {len(warnings)} warning(s).", summary_tag)

        self._notebook.select(0)

    def _cmd_save_code(self) -> None:
        """Save JSON from code editor back to registry and disk."""
        if not self._require_rm():
            return
        
        # Validate JSON first
        if not self._validate_and_color_code_tab():
            messagebox.showerror("Invalid JSON", "Fix the JSON errors before saving.")
            return
        
        try:
            code_text = self._code_text.get("1.0", "end").strip()
            new_data = json.loads(code_text)
            
            # Update registry data
            self._rm.data = new_data
            
            # Update checksum based on the exact user-provided data
            self._rm.update_checksum()
            self._rm.save()
            
            # As explicitly requested, explicitly reset indicators on "save" in code editor
            self._settings.pop("published_checksum", None)
            self._settings.pop("synced_checksum", None)
            self._save_settings()
            
            # Reload from disk to ensure what we display matches what was written
            self._rm.load()
            
            self._log_line("─" * 60)
            self._log_line("✓ Code saved to registry.", "success")
            self._refresh_ui()
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))

    def _cmd_update_editor_checksum(self) -> None:
        """Update the checksum in the code editor text."""
        code_text = self._code_text.get("1.0", "end").strip()
        if not code_text:
            return
        try:
            data = json.loads(code_text)
            
            # Update metadata
            data["checksum"] = compute_registry_checksum(data)
            
            # Write back
            json_str = json.dumps(data, indent=4, ensure_ascii=False)
            self._code_text.delete("1.0", "end")
            self._code_text.insert("1.0", json_str)
            self._code_status_var.set("✓ Checksum updated")
        except Exception as exc:
            messagebox.showerror("Update error", f"Could not update checksum: {exc}")

    def _cmd_verify_editor_json(self) -> None:
        """Manually trigger JSON validation for the editor content."""
        code_text = self._code_text.get("1.0", "end").strip()
        if not code_text:
            return
            
        try:
            data = json.loads(code_text)
            # Full validation including schema
            validate_schema(data)
            self._code_status_var.set("✓ Valid JSON & schema")
            messagebox.showinfo("JSON Valid", "The current JSON is valid and conforms to the schema.")
        except json.JSONDecodeError as e:
            self._code_status_var.set(f"✗ Invalid JSON: {e.msg}")
            messagebox.showerror("JSON Invalid", f"Invalid JSON format: {e.msg}")
        except Exception as e:
            self._code_status_var.set(f"✗ Schema error: {str(e)}")
            messagebox.showerror("Schema Error", f"JSON does not conform to schema:\n{str(e)}")

    def _show_all_options_help(self) -> None:
        """Show a popup explaining all command options for all tabs."""
        help_text = """WORKFLOW & COMMAND REFERENCE

╔════════════════════════════════════════════════════════════════╗
║                        TYPICAL WORKFLOW                        ║
╚════════════════════════════════════════════════════════════════╝

STEP 1: LOAD REGISTRY (Header)
  • Enter path to registry JSON file in text field at top
  • Click "Browse…" to select file, or type path directly
  • Click "Load" to load the registry into memory
  • File is locked in memory for all subsequent operations
  → Must do FIRST before any other operations

STEP 2: VALIDATE REGISTRY (Status & Validate tab)
  • Click "Refresh Status" to see current registry metadata
  • Use --strict checkbox: unchecked = warnings OK, checked = block warnings
  • Use --skip-artifacts: unchecked = verify files exist, checked = skip check
  • Click "Validate" to run validation
  → DO THIS after loading, before making changes (optional but recommended)

STEP 3A: ADD FIRMWARE (optional, Add Firmware tab)
  • Enter firmware version (semver: e.g., 2.1.0)
  • Browse and select .lbf firmware binary file
  • S3 Path auto-filled (or customize if needed)
  • Click "Add Firmware" to register in compatibility registry
  → Adds firmware version & computes SHA-256 checksum
  → Auto-updates schemaVersion, timestamps, and master checksum

STEP 3B: ADD APP VERSION (optional, Add App tab)
  • Enter app version (semver: e.g., 3.0.1)
  • Fill 4 protocol/datamodel version fields
  • Click "Add App" to register in compatibility registry
  → Auto-updates schemaVersion, timestamps, and master checksum

STEP 3C: ADD SCRIPTS (optional, Add Ableton Script & Add Reaper Script tabs)
  • For each DAW (Ableton and/or Reaper):
    ◦ Enter script version (e.g., 1.5.2)
    ◦ Browse and select .zip script package file
    ◦ S3 Path auto-filled (or customize if needed)
    ◦ Click "Add [DAW] Script" to register
  → Auto-computes SHA-256, copies .zip to artifacts/, updates registry

STEP 4: ADD PAIRS (optional, Pairs tab)
  • Use only if versions need compatibility constraints
  • Enter axis name (e.g., firmware_ableton_script)
  • Enter left-hand version and right-hand version
  • Click "Add Pair" to define compatibility relationship
  → Example: firmware 2.0 ↔ ableton_script 1.5 are compatible

STEP 5: VALIDATE AGAIN (Status & Validate tab)
  • After adding firmware/app/scripts/pairs, validate again
  • This ensures all changes are syntactically correct
  • Use --strict if you want to enforce no warnings

STEP 6: PUBLISH TO S3 (optional, Publish tab)
  • Ensure S3 credentials set in environment (OS_PROJECT_NAME, etc.)
  • Checkboxes:
    ◦ --dry-run: unchecked = upload, checked = simulate only
    ◦ --strict: unchecked = OK with warnings, checked = block warnings
  • Click "Publish" to upload registry + artifacts to S3
  → First imports credentials, then uploads firmware/scripts, then registry
  → USE --dry-run FIRST to test without uploading

STEP 7: SYNC TO GOOROOLINK (optional, Sync tab)
  • Source: path to registry file (default: registry/compatibility_registry.json)
  • Target: destination path (default: ~/Dev/GoorooLink/GoorooLinkContent/assets/)
  • Click "Sync" to copy registry to target location
  → Embeds registry in GoorooLink for distribution

╔════════════════════════════════════════════════════════════════╗
║                     COMMAND OPTIONS DETAILS                    ║
╚════════════════════════════════════════════════════════════════╝

STATUS & VALIDATE TAB
──────────────────────────────────────────────────────────────────
--strict
  • unchecked: Errors block validation, warnings allowed
  • checked: Both errors AND warnings block validation

--skip-artifacts
  • unchecked: Checks artifact files (.lbf, .zip) exist on disk
  • checked: Skips file existence check (faster)

ADD FIRMWARE TAB
──────────────────────────────────────────────────────────────────
Version: Semver format (e.g., 1.0.0, 2.3.1)
File: .lbf firmware binary file (required)
S3 Path: Custom S3 location (default: /firmware/FILE_NAME)

ADD APP TAB
──────────────────────────────────────────────────────────────────
Version: Semver format (e.g., 2.1.0, required)
gprotocol_version: Gprotocol version (required)
device_datamodel_version: Device datamodel version (required)
gprotocol_std_command_set_version: Standard command set (required)
gprotocol_dev_command_set_version: Dev command set (required)

ADD SCRIPT TABS (Ableton & Reaper)
──────────────────────────────────────────────────────────────────
Version: Script version (e.g., 1.2.3, required)
File: .zip package containing script (required)
S3 Path: Custom S3 location (default: /scripts/DAW/FILE.zip)

PAIRS TAB
──────────────────────────────────────────────────────────────────
Axis: Axis identifier (e.g., firmware_ableton_script, required)
Left Version: Left-hand version (required)
Right Version: Right-hand version (required)
Purpose: Define compatibility between two versions

PUBLISH TAB
──────────────────────────────────────────────────────────────────
--dry-run
  • unchecked: Executes actual S3 publish
  • checked: Simulates publish without uploading (safe to test first)

--strict
  • unchecked: Publishes if no errors (warnings OK)
  • checked: Refuses publish if ANY warnings exist

S3 Credentials: Set via environment variables
  • OS_PROJECT_NAME, OS_USERNAME, OS_PASSWORD, OS_AUTH_URL

SYNC TAB
──────────────────────────────────────────────────────────────────
Source: Registry file path (default: registry/compatibility_registry.json)
Target: Destination path (default: ~/Dev/GoorooLink/GoorooLinkContent/assets/)
Purpose: Copy validated registry to GoorooLink for embedding

╔════════════════════════════════════════════════════════════════╗
║                         TIPS & NOTES                           ║
╚════════════════════════════════════════════════════════════════╝

• All field values persist automatically between sessions
• Registry metadata (version, timestamps, checksum) auto-update
• Artifact files (.lbf, .zip) auto-copied to artifacts/ directory
• Use --dry-run on Publish before committing to real S3
• Validate after each major edit to catch errors early
• Pairs are optional; only use if versions have constraints
"""
        
        popup = tk.Toplevel(self.root)
        popup.title("Workflow & Options Guide")
        popup.geometry("720x700")
        
        text_widget = tk.Text(popup, wrap="word", padx=10, pady=10, font=("Monaco", 9))
        text_widget.insert(1.0, help_text)
        text_widget.config(state="disabled")
        text_widget.pack(fill="both", expand=True)
        
        ttk.Button(popup, text="Close", command=popup.destroy).pack(pady=10)

    def _cmd_increment_version(self) -> None:
        if not self._require_rm():
            return
        rm = self._rm
        old_ver = rm.data.get("schemaVersion")
        new_ver = rm.bump_schema_version()
        rm.update_generated_at()
        rm.update_checksum()
        rm.save()
        
        self._settings.pop("published_checksum", None)
        self._settings.pop("synced_checksum", None)
        self._save_settings()
        self._log_line("─" * 60)
        self._log_line(f"✓ Manual version increment. schemaVersion: {old_ver} → {new_ver}", "success")
        self._refresh_ui()

    def _cmd_add_firmware(self) -> None:
        if not self._require_rm():
            return
        version  = self._fw_version_var.get().strip()
        file_str = self._fw_file_var.get().strip()
        s3_path  = self._fw_s3_path_var.get().strip() or None

        if not version:
            messagebox.showerror("Validation", "Version is required.")
            return
        if not file_str:
            messagebox.showerror("Validation", ".lbf file path is required.")
            return
        src = Path(file_str)
        if not src.exists():
            messagebox.showerror("File not found", str(src))
            return

        self._log_line("─" * 60)
        self._log_line(f"Adding firmware {version}…")
        rm = self._rm

        def _run():
            _s3 = s3_path or f"/firmware/Liobox2/{src.name}"
            # Ensure the S3 path points to a file, not a directory
            if _s3.endswith("/"):
                _s3 = f"{_s3.rstrip('/')}/{src.name}"
            
            print(f"Computing SHA256 of {src.name}…")
            checksum = compute_file_checksum(src)
            print(f"  {checksum}")

            rm.add_firmware(version, _s3, checksum)

            dest = rm.path.parent.parent / "artifacts" / "firmware" / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
                print(f"Copied {src.name} → {dest}")

            paired_fw = {
                fw
                for fw_list in rm.data.get("axes", {}).get("app_firmware", {}).get("pairs", {}).values()
                for fw in fw_list
            }
            if version not in paired_fw:
                print(f"\n⚠ Warning: firmware {version} is not paired with any app version.")
                print(f"  Use the Pairs tab: app_firmware <app_version> {version}")

            rm.update_generated_at()
            rm.update_checksum()
            rm.save()
            print(f"\n✓ Registry saved.")
            self._save_settings()

        self._run_in_thread(_run)

    def _cmd_add_app(self) -> None:
        if not self._require_rm():
            return
        version   = self._app_version_var.get().strip()
        gprot     = self._app_gprot_var.get().strip()
        datamodel = self._app_datamodel_var.get().strip()
        std_cmd   = self._app_std_var.get().strip()
        dev_cmd   = self._app_dev_var.get().strip()

        for name, val in [("App version", version), ("gprotocol", gprot),
                          ("datamodel", datamodel), ("std-cmd", std_cmd), ("dev-cmd", dev_cmd)]:
            if not val:
                messagebox.showerror("Validation", f"{name} is required.")
                return

        self._log_line("─" * 60)
        self._log_line(f"Adding app version {version}…")
        rm = self._rm

        def _run():
            rm.add_app_version(version, {
                "gprotocol_version": gprot,
                "device_datamodel_version": datamodel,
                "gprotocol_std_command_set_version": std_cmd,
                "gprotocol_dev_command_set_version": dev_cmd,
            })
            rm.update_generated_at()
            rm.update_checksum()
            rm.save()
            print(f"✓ Added app version {version}.")
            self._save_settings()

        self._run_in_thread(_run)

    def _cmd_add_script_ableton(self) -> None:
        self._cmd_add_script_impl("ableton", self._script_ableton_version_var,
                                   self._script_ableton_file_var, self._ableton_s3_path_var)

    def _cmd_add_script_reaper(self) -> None:
        self._cmd_add_script_impl("reaper", self._script_reaper_version_var,
                                   self._script_reaper_file_var, self._reaper_s3_path_var)

    def _cmd_add_script_impl(self, daw: str, version_var: tk.StringVar,
                             file_var: tk.StringVar, s3_var: tk.StringVar) -> None:
        """Implementation shared by both ableton and reaper script commands."""
        if not self._require_rm():
            return
        version  = version_var.get().strip()
        file_str = file_var.get().strip()
        s3_path  = s3_var.get().strip() or None

        if not version:
            messagebox.showerror("Validation", "Version is required.")
            return
        if not file_str:
            messagebox.showerror("Validation", ".zip file path is required.")
            return
        src = Path(file_str)
        if not src.exists():
            messagebox.showerror("File not found", str(src))
            return

        axis = f"firmware_{daw}_script"
        self._log_line("─" * 60)
        self._log_line(f"Adding {daw} script {version}…")
        rm = self._rm

        def _run():
            _s3 = s3_path or f"/scripts/{daw.capitalize()}/{src.name}"
            # Ensure the S3 path points to a file, not a directory
            if _s3.endswith("/"):
                _s3 = f"{_s3.rstrip('/')}/{src.name}"
                
            print(f"Computing SHA256 of {src.name}…")
            checksum = compute_file_checksum(src)
            print(f"  {checksum}")

            rm.add_script(axis, version, _s3, checksum)

            dest = rm.path.parent.parent / "artifacts" / "scripts" / daw / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
                print(f"Copied {src.name} → {dest}")

            rm.update_generated_at()
            rm.update_checksum()
            rm.save()
            print(f"✓ Script {version} added.")
            self._save_settings()

        self._run_in_thread(_run)

    def _cmd_add_pair(self) -> None:
        if not self._require_rm():
            return
        axis  = self._pair_axis_var.get().strip()
        left  = self._pair_left_var.get().strip()
        right = self._pair_right_var.get().strip()

        if not all([axis, left, right]):
            messagebox.showerror("Validation", "Axis, left version, and right version are required.")
            return

        self._log_line("─" * 60)
        self._log_line(f"Adding pair [{axis}] {left} ↔ {right}…")
        rm = self._rm

        def _run():
            rm.add_pair(axis, left, right)
            rm.update_generated_at()
            rm.update_checksum()
            rm.save()
            print(f"✓ Pair added.")
            self._save_settings()

        self._run_in_thread(_run)

    def _cmd_remove_pair(self) -> None:
        if not self._require_rm():
            return
        axis  = self._pair_axis_var.get().strip()
        left  = self._pair_left_var.get().strip()
        right = self._pair_right_var.get().strip()

        if not all([axis, left, right]):
            messagebox.showerror("Validation", "Axis, left version, and right version are required.")
            return
        if not messagebox.askyesno("Confirm removal",
                                   f"Remove pair from [{axis}]:\n  {left}  ↔  {right}"):
            return

        self._log_line("─" * 60)
        self._log_line(f"Removing pair [{axis}] {left} ↔ {right}…")
        rm = self._rm

        def _run():
            rm.remove_pair(axis, left, right)
            rm.update_generated_at()
            rm.update_checksum()
            rm.save()
            print(f"✓ Pair removed.")
            self._save_settings()

        self._run_in_thread(_run)

    def _cmd_list_remote(self) -> None:
        """List objects in the remote S3 container via the swift CLI."""
        password     = os.environ.get("OS_PASSWORD", "").strip()
        auth_url     = os.environ.get("OS_AUTH_URL", "").strip()
        username     = os.environ.get("OS_USERNAME", "").strip()
        project_name = (os.environ.get("OS_PROJECT_NAME") or os.environ.get("OS_TENANT_NAME", "")).strip()

        missing = [name for name, val in {
            "OS_PASSWORD":    password,
            "OS_AUTH_URL":    auth_url,
            "OS_USERNAME":    username,
            "OS_PROJECT_NAME": project_name,
        }.items() if not val]

        if missing:
            messagebox.showerror(
                "Missing credentials",
                "The following environment variables are not set:\n  "
                + "\n  ".join(missing)
                + "\n\nLoad your OpenStack credentials first.",
            )
            return

        self._log_line("─" * 60)
        self._log_line("Listing remote objects (swift list app-updates)…")

        def _run():
            cmd = ["swift", "list", "app-updates"]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError:
                print(
                    "✗ 'swift' CLI not found.\n"
                    "  Install it with: pip install python-swiftclient"
                )
                return

            if result.returncode != 0:
                print(f"✗ swift list failed (exit {result.returncode}):")
                print(result.stderr.strip() or "(no error output)")
                return

            output = result.stdout.strip()
            if not output:
                print("(container is empty)")
            else:
                print(output)

        self._run_in_thread(_run)

    def _cmd_publish(self) -> None:
        if not self._require_rm():
            return
        dry_run       = self._pub_dry_run_var.get()
        strict        = self._pub_strict_var.get()
        skip_validate = self._pub_skip_val_var.get()

        fw_folder = Path(self._fw_folder_var.get().strip()) if self._fw_folder_var.get().strip() else None
        app_folder = Path(self._app_folder_var.get().strip()) if self._app_folder_var.get().strip() else None
        ableton_scripts_folder = Path(self._ableton_scripts_folder_var.get().strip()) if self._ableton_scripts_folder_var.get().strip() else None
        reaper_scripts_folder = Path(self._reaper_scripts_folder_var.get().strip()) if self._reaper_scripts_folder_var.get().strip() else None

        # We no longer require explicit folders; publisher now automatically falls back to artifacts/ directory

        rm = self._rm
        self._log_line("─" * 60)
        self._log_line(f"{'[dry-run] ' if dry_run else ''}Preparing publish…")

        # ── Phase 1 (thread): fetch remote listing + build plan ──────────
        def _plan():
            publisher = S3Publisher(dry_run=dry_run)

            print("Fetching remote object listing…")
            remote_set = S3Publisher(dry_run=False).get_remote_set()
            print(f"  {len(remote_set)} object(s) found on remote.")
            if dry_run:
                print("[dry-run] Remote listing fetched for accurate diff — nothing will be uploaded.")

            print("Scanning local folders…")
            plan = publisher.build_plan(
                rm,
                remote_set=remote_set,
                fw_folder=fw_folder,
                app_folder=app_folder,
                ableton_scripts_folder=ableton_scripts_folder,
                reaper_scripts_folder=reaper_scripts_folder,
                fw_s3_path=self._fw_s3_path_var.get(),
                app_appcast_s3_path=self._app_appcast_s3_path_var.get(),
                app_versions_s3_path=self._app_versions_s3_path_var.get(),
                ableton_s3_path=self._ableton_s3_path_var.get(),
                reaper_s3_path=self._reaper_s3_path_var.get(),
                registry_s3_path=self._registry_s3_path_var.get(),
            )

            # Print plan to log
            if plan.to_upload:
                print(f"\nTo upload ({len(plan.to_upload)} file(s)) + registry:")
                for _, remote_path, label in plan.to_upload:
                    print(f"  • {label} → {remote_path}")
            else:
                print("\nNo new local files to upload.")

            if plan.already_remote:
                print(f"\nAlready on S3 — will be skipped ({len(plan.already_remote)}):")
                for label in plan.already_remote:
                    print(f"  ⊘ {label}")

            if plan.missing:
                print(f"\n⚠ Missing — neither local nor remote ({len(plan.missing)}):")
                for label in plan.missing:
                    print(f"  ✗ {label}")

            # Hand off to main thread for confirmation dialog
            self.root.after(0, lambda: self._on_publish_plan_ready(
                plan, publisher, dry_run, strict, skip_validate
            ))

        self._run_in_thread(_plan)

    def _on_publish_plan_ready(
        self,
        plan: PublishPlan,
        publisher: S3Publisher,
        dry_run: bool,
        strict: bool,
        skip_validate: bool,
    ) -> None:
        """Called on the main thread once the publish plan is ready."""
        if not plan.to_upload:
            messagebox.showinfo(
                "Nothing to upload",
                "All artifacts are already on the remote server." if not dry_run
                else "[dry-run] No local files found to upload.",
            )
            return

        dlg = _PublishConfirmDialog(
            self.root,
            to_upload=plan.to_upload,
            already_remote=plan.already_remote,
            missing=plan.missing,
            appcast_overwrites_remote=plan.appcast_overwrites_remote,
            dry_run=dry_run,
        )
        if dlg.result is None:
            return  # user cancelled
        if not dlg.result:
            self._log_line("No files selected — publish aborted.")
            return

        # Filter plan to only the files the user checked
        plan.to_upload = dlg.result

        rm = self._rm
        artifacts_dir = rm.path.parent.parent / "artifacts"

        # ── Phase 2 (thread): validate + execute plan ────────────────────
        def _upload():
            if not skip_validate:
                print("Validating registry…")
                issues = validate_all(rm.data, artifacts_dir=artifacts_dir, allow_missing_artifacts=True)
                errors = [i for i in issues if i.severity == Severity.ERROR]
                warnings = [i for i in issues if i.severity == Severity.WARNING]
                if errors:
                    print(f"✗ {len(errors)} error(s) — cannot publish.")
                    for i in errors:
                        print(f"  ✗ {i.message}")
                    return
                if warnings:
                    print(f"⚠ {len(warnings)} warning(s):")
                    for i in warnings:
                        print(f"  ⚠ {i.message}")
                    if strict:
                        print("✗ Refusing publish in --strict mode (warnings present).")
                        return
                print(f"  ✓ Validation passed ({len(errors)} error(s), {len(warnings)} warning(s)).\n")

            publisher.execute_plan(plan)
            if dry_run:
                print("\n(dry-run complete — nothing was uploaded)")
            else:
                self._settings["published_checksum"] = rm.data.get("checksum", "")
                self._save_settings()
                self.root.after(0, self._refresh_ui)
                print("\n✓ Publish complete!")

        self._run_in_thread(_upload)

    def _cmd_sync(self) -> None:
        if not self._require_rm():
            return
        target = self._sync_target_var.get().strip()
        if not target:
            messagebox.showerror("Validation", "Target directory is required.")
            return

        self._log_line("─" * 60)
        self._log_line(f"Syncing to {target}…")
        rm = self._rm

        def _run():
            target_dir = Path(target)
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / "compatibility_registry.json"
            shutil.copy2(rm.path, dest)
            print(f"✓ Copied {rm.path} → {dest}")
            self._settings["synced_checksum"] = rm.data.get("checksum", "")
            self._save_settings()
            self.root.after(0, self._refresh_ui)

        self._run_in_thread(_run)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    GoorooRegistryGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
