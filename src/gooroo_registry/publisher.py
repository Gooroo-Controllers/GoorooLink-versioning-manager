"""S3 publisher: upload artifacts and the registry to Infomaniak S3 via Swift."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .registry import CompatibilityRegistryManager


@dataclass
class PublishPlan:
    """Result of comparing registry entries against the remote server and local folders."""

    # Files to upload: local file exists and is not yet on the remote.
    to_upload: list[tuple[Path, str, str]] = field(default_factory=list)
    # Already on remote (by path) — will be skipped.
    already_remote: list[str] = field(default_factory=list)
    # In registry, found neither locally nor on the remote.
    missing: list[str] = field(default_factory=list)
    # appcast.xml already exists on remote and will be overwritten.
    appcast_overwrites_remote: bool = False
    remote_appcast_path: str = ""

    registry_local_path: Path = field(default_factory=Path)
    registry_s3_path: str = ""


class S3Publisher:
    """Upload firmware, script archives, and the registry to Infomaniak S3."""

    CONTAINER = "app-updates"
    REGISTRY_OBJECT = "software/GoorooLink/production/compatibility_registry.json"

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._conn = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_connection(self):
        if self._conn is not None:
            return self._conn

        try:
            import swiftclient  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "python-swiftclient is required for publishing.\n"
                "Run: pip install python-swiftclient python-keystoneclient"
            )

        auth_url = os.environ.get("OS_AUTH_URL")
        username = os.environ.get("OS_USERNAME")
        password = os.environ.get("OS_PASSWORD")
        project_name = os.environ.get("OS_PROJECT_NAME") or os.environ.get("OS_TENANT_NAME")
        user_domain_name = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
        project_domain_name = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
        region_name = os.environ.get("OS_REGION_NAME")

        missing = [
            name
            for name, val in {
                "OS_AUTH_URL": auth_url,
                "OS_USERNAME": username,
                "OS_PASSWORD": password,
                "OS_PROJECT_NAME": project_name,
            }.items()
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"Missing OpenStack credentials: {', '.join(missing)}\n"
                "Source config/openstack_env.sh before publishing."
            )

        import swiftclient

        self._conn = swiftclient.Connection(
            authurl=auth_url,
            user=username,
            key=password,
            os_options={
                "user_domain_name": user_domain_name,
                "project_domain_name": project_domain_name,
                "project_name": project_name,
                "region_name": region_name,
            },
            auth_version="3",
        )
        return self._conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_remote_set(self) -> set[str]:
        """Fetch all object names from the remote container in one API call."""
        conn = self._get_connection()
        _headers, objects = conn.get_container(self.CONTAINER)
        return {obj["name"] for obj in objects}

    def build_plan(
        self,
        registry: CompatibilityRegistryManager,
        remote_set: set[str],
        fw_folder: Optional[Path] = None,
        app_folder: Optional[Path] = None,
        ableton_scripts_folder: Optional[Path] = None,
        reaper_scripts_folder: Optional[Path] = None,
        fw_s3_path: str = "firmware/Liobox2/",
        app_appcast_s3_path: str = "software/GoorooLink/production/appcast.xml",
        app_versions_s3_path: str = "software/GoorooLink/production/",
        ableton_s3_path: str = "scripts/Ableton/",
        reaper_s3_path: str = "scripts/Reaper/",
        registry_s3_path: Optional[str] = None,
    ) -> PublishPlan:
        """Compare registry entries against the remote set and local folders.

        Three-way categorisation for each artifact:
          - remote path already in remote_set     → already_remote (skip)
          - local file exists, not in remote_set  → to_upload
          - not local, not remote                 → missing (warning)

        The registry JSON is NOT in the plan's to_upload list; it is handled
        separately by execute_plan() and is always uploaded last.
        """
        registry_s3_path = registry_s3_path or self.REGISTRY_OBJECT
        plan = PublishPlan(
            registry_local_path=registry.path,
            registry_s3_path=registry_s3_path,
        )

        # ── Firmware ───────────────────────────────────────────────────
        for fw_ver, fw_data in registry.data.get("firmware", {}).items():
            path_str = fw_data["path"]
            remote_path = path_str.lstrip("/")
            filename = Path(path_str).name
            
            if path_str.endswith("/"):
                label = f"firmware {fw_ver}: [DIRECTORY ERROR] {filename}/"
            else:
                label = f"firmware {fw_ver}: {filename}"
            if fw_folder:
                local_path = fw_folder / filename
            else:
                local_path = registry.path.parent.parent / "artifacts" / "firmware" / filename

            if remote_path in remote_set:
                plan.already_remote.append(label)
            elif local_path and local_path.exists() and not path_str.endswith("/"):
                plan.to_upload.append((local_path, remote_path, label))
            else:
                plan.missing.append(label)

        # ── App ─────────────────────────────────────────────────────────
        if app_folder and app_folder.exists():
            # appcast.xml — always re-upload (it changes with every release)
            appcast_path = app_folder / "appcast.xml"
            if appcast_path.exists():
                plan.remote_appcast_path = app_appcast_s3_path
                if app_appcast_s3_path in remote_set:
                    plan.appcast_overwrites_remote = True
                plan.to_upload.append((appcast_path, app_appcast_s3_path, "appcast.xml"))

            for app_ver in registry.data.get("protocol_requirements", {}).keys():
                candidates = list(app_folder.glob(f"*{app_ver}*.zip"))
                if candidates:
                    app_zip = candidates[0]
                    remote_path = f"{app_versions_s3_path}{app_zip.name}"
                    label = f"app {app_ver}: {app_zip.name}"
                    if remote_path in remote_set:
                        plan.already_remote.append(label)
                    else:
                        plan.to_upload.append((app_zip, remote_path, label))
                else:
                    # Check whether a matching zip is already on the remote
                    remote_match = next(
                        (
                            p for p in remote_set
                            if p.startswith(app_versions_s3_path)
                            and app_ver in p
                            and p.endswith(".zip")
                        ),
                        None,
                    )
                    if remote_match:
                        plan.already_remote.append(
                            f"app {app_ver}: {Path(remote_match).name} (remote only)"
                        )
                    else:
                        plan.missing.append(f"app {app_ver}: no .zip found locally or remotely")

        # ── Scripts ─────────────────────────────────────────────────────
        for axis_name, axis_data in registry.data.get("axes", {}).items():
            for script_ver, script_data in axis_data.get("available_scripts", {}).items():
                path_str = script_data["path"]
                remote_path = path_str.lstrip("/")
                parts = path_str.lstrip("/").split("/")
                if len(parts) < 3:
                    continue
                daw = parts[1].lower()       # "ableton" or "reaper"
                filename = parts[-1]
                
                if path_str.endswith("/"):
                    label = f"{daw} script {script_ver}: [DIRECTORY ERROR] {filename}/"
                else:
                    label = f"{daw} script {script_ver}: {filename}"
                
                local_folder = ableton_scripts_folder if daw == "ableton" else reaper_scripts_folder
                if local_folder:
                    local_path = local_folder / daw / filename
                else:
                    local_path = registry.path.parent.parent / "artifacts" / "scripts" / daw / filename

                # Fallback: if a custom folder was given but the file isn't there,
                # also check the default artifacts/ path (where add-script always copies to).
                default_path = registry.path.parent.parent / "artifacts" / "scripts" / daw / filename
                if local_folder and not local_path.exists() and default_path.exists():
                    local_path = default_path

                if remote_path in remote_set:
                    plan.already_remote.append(label)
                elif local_path and local_path.exists() and not path_str.endswith("/"):
                    plan.to_upload.append((local_path, remote_path, label))
                else:
                    plan.missing.append(label)

        return plan

    def execute_plan(self, plan: PublishPlan) -> None:
        """Upload all files in the plan, then the registry (always last)."""
        if plan.appcast_overwrites_remote and plan.remote_appcast_path:
            self._backup_remote_appcast(plan.remote_appcast_path)

        if plan.to_upload:
            print(f"Uploading {len(plan.to_upload)} artifact(s)…")
            for local_path, remote_path, label in plan.to_upload:
                self.upload_artifact(local_path, remote_path)
        else:
            print("No new artifacts to upload.")

        if plan.already_remote:
            print(f"\nSkipped {len(plan.already_remote)} file(s) already on S3:")
            for label in plan.already_remote:
                print(f"  ⊘ {label}")

        if plan.missing:
            print(f"\n⚠ {len(plan.missing)} file(s) missing (neither local nor remote):")
            for label in plan.missing:
                print(f"  ✗ {label}")

        print("\nRegistry:")
        self.upload_registry(plan.registry_local_path, plan.registry_s3_path)

        total = len(plan.to_upload) + 1  # +1 for registry
        print(f"\n✓ Done — {total} file(s) uploaded.")

    def list_remote_artifacts(self) -> list[str]:
        """List all object names in the remote container."""
        if self.dry_run:
            return ["(dry-run mode — cannot list remote)"]
        try:
            remote_set = self.get_remote_set()
            return sorted(remote_set) if remote_set else ["(container is empty)"]
        except Exception as exc:
            return [f"Error listing remote: {exc}"]

    def upload_artifact(self, local_path: Path, remote_path: str) -> None:
        size_kb = local_path.stat().st_size // 1024
        if self.dry_run:
            print(f"  [dry-run] Would upload {local_path.name} ({size_kb} KB) → {remote_path}")
            return
        conn = self._get_connection()
        with open(local_path, "rb") as fh:
            conn.put_object(
                self.CONTAINER,
                remote_path,
                contents=fh,
                content_length=local_path.stat().st_size,
            )
        print(f"  ✓ Uploaded {local_path.name} ({size_kb} KB) → {remote_path}")

    def upload_registry(self, registry_path: Path, registry_s3_path: Optional[str] = None) -> None:
        if registry_s3_path is None:
            registry_s3_path = self.REGISTRY_OBJECT
        if self.dry_run:
            print(f"  [dry-run] Would upload compatibility_registry.json → {registry_s3_path}")
            return
        conn = self._get_connection()
        content = registry_path.read_bytes()
        conn.put_object(
            self.CONTAINER,
            registry_s3_path,
            contents=content,
            content_type="application/json; charset=utf-8",
        )
        print(f"  ✓ Uploaded registry → {registry_s3_path}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _backup_remote_appcast(self, remote_appcast_path: str) -> None:
        if self.dry_run:
            print("  [dry-run] Would backup remote appcast.xml before overwriting")
            return
            
        print("  Backing up existing remote appcast.xml…")
        conn = self._get_connection()
        try:
            _headers, body = conn.get_object(self.CONTAINER, remote_appcast_path)
            content = body.decode("utf-8")
            import re
            m = re.search(r"<sparkle:shortVersionString>([^<]+)</sparkle:shortVersionString>", content)
            if not m:
                m = re.search(r"<sparkle:version>([^<]+)</sparkle:version>", content)
                
            if m:
                version = m.group(1)
                new_path = remote_appcast_path.replace("appcast.xml", f"appcast_{version}.xml")
                conn.put_object(
                    self.CONTAINER,
                    new_path,
                    contents=body,
                    content_type="application/xml",
                )
                print(f"  ✓ Remote appcast backed up as {new_path}")
            else:
                print("  ⚠ Could not determine version from remote appcast.xml, skipped backup.")
        except Exception as e:
            print(f"  ⚠ Failed to backup remote appcast.xml: {e}")

    def _remote_exists(self, conn, object_path: str) -> bool:
        try:
            conn.head_object(self.CONTAINER, object_path)
            return True
        except Exception:
            return False
