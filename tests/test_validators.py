import copy
import json

import pytest

from gooroo_registry.checksum import compute_registry_checksum
from gooroo_registry.validators import Severity, validate_all


# ------------------------------------------------------------------
# Happy path
# ------------------------------------------------------------------


def test_valid_registry_has_no_errors(valid_registry):
    issues = validate_all(valid_registry)
    errors = [i for i in issues if i.severity == Severity.ERROR]
    assert not errors, [i.message for i in errors]


# ------------------------------------------------------------------
# Checksum integrity
# ------------------------------------------------------------------


def test_wrong_checksum_is_error(valid_registry):
    data = copy.deepcopy(valid_registry)
    data["checksum"] = "sha256:" + "0" * 64
    issues = validate_all(data)
    assert any(i.rule == "checksum_integrity" and i.severity == Severity.ERROR for i in issues)


# ------------------------------------------------------------------
# Schema conformance
# ------------------------------------------------------------------


def test_missing_required_field_is_error(valid_registry):
    data = copy.deepcopy(valid_registry)
    del data["schemaVersion"]
    issues = validate_all(data)
    assert any(i.rule == "schema_conformance" and i.severity == Severity.ERROR for i in issues)


# ------------------------------------------------------------------
# Version format
# ------------------------------------------------------------------


def test_invalid_firmware_version_format(valid_registry):
    data = copy.deepcopy(valid_registry)
    data["firmware"]["not-semver"] = {
        "version": "not-semver",
        "path": "/firmware/Liobox2/test.lbf",
        "checksum": "sha256:" + "a" * 64,
    }
    data["checksum"] = compute_registry_checksum(data)
    issues = validate_all(data)
    assert any(i.rule == "version_format" and i.severity == Severity.ERROR for i in issues)


# ------------------------------------------------------------------
# Orphan firmware
# ------------------------------------------------------------------


def test_unpaired_firmware_is_warning(valid_registry):
    data = copy.deepcopy(valid_registry)
    data["firmware"]["9.9.9"] = {
        "version": "9.9.9",
        "path": "/firmware/Liobox2/liobox2_v999.lbf",
        "checksum": "sha256:" + "a" * 64,
    }
    data["checksum"] = compute_registry_checksum(data)
    issues = validate_all(data)
    warnings = [
        i for i in issues if i.severity == Severity.WARNING and "9.9.9" in i.message and "app_firmware" in i.message
    ]
    assert warnings


# ------------------------------------------------------------------
# Orphan app version (missing protocol_requirements)
# ------------------------------------------------------------------


def test_app_version_without_proto_reqs_is_warning(valid_registry):
    data = copy.deepcopy(valid_registry)
    # Add a pair without a corresponding protocol_requirements entry
    data["axes"]["app_firmware"]["pairs"]["9.9.9-beta"] = ["6.1.6"]
    data["checksum"] = compute_registry_checksum(data)
    issues = validate_all(data)
    warnings = [i for i in issues if i.severity == Severity.WARNING and "9.9.9-beta" in i.message]
    assert warnings


# ------------------------------------------------------------------
# Artifact file existence
# ------------------------------------------------------------------


def test_missing_artifact_file_is_error(valid_registry, tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "firmware").mkdir()
    issues = validate_all(valid_registry, artifacts_dir=artifacts_dir)
    errors = [i for i in issues if i.rule == "download_path_exists" and i.severity == Severity.ERROR]
    assert errors  # all firmware files are missing from empty artifacts/


def test_present_artifact_file_passes(valid_registry, tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    fw_dir = artifacts_dir / "firmware"
    fw_dir.mkdir(parents=True)
    scripts_ableton = artifacts_dir / "scripts" / "ableton"
    scripts_ableton.mkdir(parents=True)
    scripts_reaper = artifacts_dir / "scripts" / "reaper"
    scripts_reaper.mkdir(parents=True)

    # Create stub files for every artifact referenced in the registry
    for fw_data in valid_registry["firmware"].values():
        filename = fw_data["path"].split("/")[-1]
        (fw_dir / filename).write_bytes(b"stub")

    for axis_data in valid_registry["axes"].values():
        for script_data in axis_data.get("available_scripts", {}).values():
            parts = script_data["path"].lstrip("/").split("/")
            daw = parts[1].lower()
            filename = parts[-1]
            target_dir = artifacts_dir / "scripts" / daw
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / filename).write_bytes(b"stub")

    issues = validate_all(valid_registry, artifacts_dir=artifacts_dir)
    errors = [i for i in issues if i.rule == "download_path_exists"]
    assert not errors


# ------------------------------------------------------------------
# Orphan script
# ------------------------------------------------------------------


def test_unpaired_script_is_warning(valid_registry):
    data = copy.deepcopy(valid_registry)
    data["axes"]["firmware_ableton_script"]["available_scripts"]["9.9.9"] = {
        "version": "9.9.9",
        "path": "/scripts/Ableton/Liobox2_AbletonScripts_9.9.9.zip",
        "checksum": "sha256:" + "b" * 64,
    }
    data["checksum"] = compute_registry_checksum(data)
    issues = validate_all(data)
    warnings = [i for i in issues if i.severity == Severity.WARNING and "9.9.9" in i.message]
    assert warnings
