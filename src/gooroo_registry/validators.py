"""Completeness and integrity validators for the compatibility registry."""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import jsonschema

from .checksum import compute_registry_checksum
from .schema import validate_schema


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class ValidationIssue:
    severity: Severity
    message: str
    rule: str


def validate_all(
    data: dict,
    *,
    artifacts_dir: Optional[Path] = None,
    strict: bool = False,
    allow_missing_artifacts: bool = False,
) -> list[ValidationIssue]:
    """Run all validators.  Returns a list of issues (errors + warnings).

    Args:
        data:                    Parsed registry dict.
        artifacts_dir:           If provided, check that artifact files exist on disk.
        strict:                  Unused here, but callers use it to decide whether to treat
                                 warnings as errors after this function returns.
        allow_missing_artifacts: If True, artifact file existence is not checked (for publish
                                 operations where files may already be in S3).
    """
    issues: list[ValidationIssue] = []

    # ── 1. Schema conformance ──────────────────────────────────────────
    try:
        validate_schema(data)
    except jsonschema.ValidationError as exc:
        issues.append(
            ValidationIssue(Severity.ERROR, f"Schema validation failed: {exc.message}", "schema_conformance")
        )
        # Without a valid schema we cannot safely run the rest of the checks.
        return issues

    # ── 2. Checksum integrity ──────────────────────────────────────────
    stored = data.get("checksum", "")
    computed = compute_registry_checksum(data)
    if stored != computed:
        issues.append(
            ValidationIssue(
                Severity.ERROR,
                f"Checksum mismatch — stored: {stored}, computed: {computed}",
                "checksum_integrity",
            )
        )

    # ── 3. Version format (loose semver) ──────────────────────────────
    semver_re = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")

    for fw_ver in data.get("firmware", {}):
        if not semver_re.match(fw_ver):
            issues.append(
                ValidationIssue(Severity.ERROR, f"Firmware version {fw_ver!r} is not valid semver", "version_format")
            )
    for app_ver in data.get("protocol_requirements", {}):
        if not semver_re.match(app_ver):
            issues.append(
                ValidationIssue(Severity.ERROR, f"App version {app_ver!r} is not valid semver", "version_format")
            )

    # ── 4. No duplicate versions ──────────────────────────────────────
    fw_versions = list(data.get("firmware", {}).keys())
    if len(fw_versions) != len(set(fw_versions)):
        issues.append(ValidationIssue(Severity.ERROR, "Duplicate firmware versions found", "no_duplicate_versions"))

    app_versions = list(data.get("protocol_requirements", {}).keys())
    if len(app_versions) != len(set(app_versions)):
        issues.append(ValidationIssue(Severity.ERROR, "Duplicate app versions found", "no_duplicate_versions"))

    # ── 5. Completeness checks ─────────────────────────────────────────
    all_fw = set(data.get("firmware", {}).keys())
    axes = data.get("axes", {})
    proto_reqs = set(data.get("protocol_requirements", {}).keys())

    # app_firmware axis
    app_fw_axis = axes.get("app_firmware", {})
    paired_fw: set[str] = set()
    for app_ver, fw_list in app_fw_axis.get("pairs", {}).items():
        paired_fw.update(fw_list)
        # Every app version in pairs must have protocol_requirements
        if app_ver not in proto_reqs:
            issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    f"App version {app_ver!r} appears in app_firmware pairs but has no protocol_requirements entry",
                    "no_orphan_app",
                )
            )

    # Every firmware must appear in at least one app_firmware pair
    for fw_ver in all_fw:
        if fw_ver not in paired_fw:
            issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    f"Firmware {fw_ver!r} has no app_firmware pair",
                    "no_orphan_firmware",
                )
            )

    # Script axes
    for axis_name, axis_data in axes.items():
        if axis_name == "app_firmware":
            continue

        available_scripts = set(axis_data.get("available_scripts", {}).keys())
        paired_scripts: set[str] = set()
        fw_in_pairs: set[str] = set()

        for fw_ver, script_list in axis_data.get("pairs", {}).items():
            fw_in_pairs.add(fw_ver)
            paired_scripts.update(script_list)

        # Firmware referenced in script pairs must exist in the firmware section
        for fw_ver in fw_in_pairs:
            if fw_ver not in all_fw:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Axis {axis_name!r}: firmware {fw_ver!r} referenced in pairs does not exist in firmware section",
                        "orphan_fw_in_script_pair",
                    )
                )

        # Every known firmware should appear in at least one script pair
        for fw_ver in all_fw:
            if fw_ver not in fw_in_pairs:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        f"Firmware {fw_ver!r} has no {axis_name} pair",
                        f"no_{axis_name}_pair",
                    )
                )

        # Every available script must be paired with at least one firmware
        for script_ver in available_scripts:
            if script_ver not in paired_scripts:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        f"Script {script_ver!r} in {axis_name!r} is not paired with any firmware",
                        "no_orphan_scripts",
                    )
                )

    # ── 6. Artifact path format ────────────────────────────────────────
    for fw_ver, fw_data in data.get("firmware", {}).items():
        path = fw_data.get("path", "")
        if path.endswith("/"):
            issues.append(ValidationIssue(Severity.ERROR, f"Firmware {fw_ver!r}: path {path!r} points to a directory, must be a file.", "valid_artifact_path"))
        elif "." not in Path(path).name:
            issues.append(ValidationIssue(Severity.WARNING, f"Firmware {fw_ver!r}: path {path!r} has no file extension.", "valid_artifact_path"))

    for axis_name, axis_data in axes.items():
        for script_ver, script_data in axis_data.get("available_scripts", {}).items():
            path = script_data.get("path", "")
            if path.endswith("/"):
                issues.append(ValidationIssue(Severity.ERROR, f"Script {script_ver!r} in {axis_name!r}: path {path!r} points to a directory.", "valid_artifact_path"))
            elif "." not in Path(path).name:
                issues.append(ValidationIssue(Severity.WARNING, f"Script {script_ver!r} in {axis_name!r}: path {path!r} has no file extension.", "valid_artifact_path"))

    # ── 7. Download path existence ─────────────────────────────────────
    if artifacts_dir is not None and not allow_missing_artifacts:
        for fw_ver, fw_data in data.get("firmware", {}).items():
            local = _resolve_artifact_path(artifacts_dir, fw_data.get("path", ""))
            if local is not None and not local.exists():
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Firmware {fw_ver!r}: artifact not found at {local}",
                        "download_path_exists",
                    )
                )
        for axis_name, axis_data in axes.items():
            for script_ver, script_data in axis_data.get("available_scripts", {}).items():
                local = _resolve_artifact_path(artifacts_dir, script_data.get("path", ""))
                if local is not None and not local.exists():
                    issues.append(
                        ValidationIssue(
                            Severity.ERROR,
                            f"Script {script_ver!r} in {axis_name!r}: artifact not found at {local}",
                            "download_path_exists",
                        )
                    )

    return issues


def _resolve_artifact_path(artifacts_dir: Path, remote_path: str) -> Optional[Path]:
    """Map an S3-style registry path to the local artifact file.

    Examples:
        /firmware/Liobox2/liobox2_v617.lbf  →  artifacts/firmware/liobox2_v617.lbf
        /scripts/Ableton/foo_3.2.0.zip       →  artifacts/scripts/ableton/foo_3.2.0.zip
    """
    if not remote_path:
        return None
    parts = remote_path.lstrip("/").split("/")
    if len(parts) < 2:
        return None
    filename = parts[-1]
    if parts[0].lower() == "firmware":
        return artifacts_dir / "firmware" / filename
    if parts[0].lower() == "scripts" and len(parts) >= 3:
        daw = parts[1].lower()
        return artifacts_dir / "scripts" / daw / filename
    return None
