import copy
import json

import pytest

from gooroo_registry.checksum import compute_registry_checksum
from gooroo_registry.registry import CompatibilityRegistryManager


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_registry_on_disk(data: dict, tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(data, indent=4), encoding="utf-8")
    rm = CompatibilityRegistryManager(p)
    rm.load()
    return rm


# ------------------------------------------------------------------
# add_firmware
# ------------------------------------------------------------------


def test_add_firmware_new_version(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.add_firmware("9.0.0", "/firmware/Liobox2/liobox2_v900.lbf", "sha256:" + "a" * 64)
    assert "9.0.0" in rm.data["firmware"]
    assert rm.data["firmware"]["9.0.0"]["checksum"] == "sha256:" + "a" * 64


def test_add_firmware_duplicate_raises(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        rm.add_firmware("6.1.6", "/firmware/Liobox2/liobox2_v616.lbf", "sha256:" + "a" * 64)


# ------------------------------------------------------------------
# add_app_version
# ------------------------------------------------------------------


def test_add_app_version(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.add_app_version(
        "0.3.0-beta",
        {
            "gprotocol_version": "3.0.0",
            "device_datamodel_version": "1.0.0",
            "gprotocol_std_command_set_version": "1.0.0",
            "gprotocol_dev_command_set_version": "2.0.0",
        },
    )
    assert "0.3.0-beta" in rm.data["protocol_requirements"]


def test_add_app_version_duplicate_raises(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        rm.add_app_version("0.2.5-beta", {"gprotocol_version": "3.0.0",
                                           "device_datamodel_version": "1.0.0",
                                           "gprotocol_std_command_set_version": "1.0.0",
                                           "gprotocol_dev_command_set_version": "2.0.0"})


# ------------------------------------------------------------------
# add_pair / remove_pair
# ------------------------------------------------------------------


def test_add_pair_new(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.add_pair("app_firmware", "0.3.0-beta", "6.1.6")
    assert "6.1.6" in rm.data["axes"]["app_firmware"]["pairs"]["0.3.0-beta"]


def test_add_pair_no_duplicates(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.add_pair("app_firmware", "0.2.5-beta", "6.1.6")  # already exists
    count = rm.data["axes"]["app_firmware"]["pairs"]["0.2.5-beta"].count("6.1.6")
    assert count == 1


def test_remove_pair(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.remove_pair("app_firmware", "0.2.5-beta", "6.1.6")
    assert "6.1.6" not in rm.data["axes"]["app_firmware"]["pairs"].get("0.2.5-beta", [])


def test_remove_pair_last_entry_removes_key(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    # Reduce 0.2.3-beta to a single fw entry, then remove it
    rm.data["axes"]["app_firmware"]["pairs"]["0.2.3-beta"] = ["6.1.6"]
    rm.remove_pair("app_firmware", "0.2.3-beta", "6.1.6")
    assert "0.2.3-beta" not in rm.data["axes"]["app_firmware"]["pairs"]


def test_remove_pair_missing_raises(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    with pytest.raises(ValueError):
        rm.remove_pair("app_firmware", "0.2.5-beta", "9.9.9")


# ------------------------------------------------------------------
# bump_schema_version
# ------------------------------------------------------------------


def test_bump_schema_version_increments_patch(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    original = rm.data["schemaVersion"]
    new = rm.bump_schema_version()
    orig_parts = list(map(int, original.split(".")))
    new_parts = list(map(int, new.split(".")))
    assert new_parts == [orig_parts[0], orig_parts[1], orig_parts[2] + 1]


# ------------------------------------------------------------------
# update_checksum
# ------------------------------------------------------------------


def test_update_checksum_reflects_mutations(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.data["firmware"]["9.0.0"] = {"version": "9.0.0", "path": "/fw/x", "checksum": "sha256:" + "0" * 64}
    checksum = rm.update_checksum()
    assert checksum == rm.data["checksum"]
    assert checksum == compute_registry_checksum(rm.data)


# ------------------------------------------------------------------
# save / reload round-trip
# ------------------------------------------------------------------


def test_save_reload_roundtrip(valid_registry, tmp_path):
    rm = _make_registry_on_disk(valid_registry, tmp_path)
    rm.add_firmware("9.0.0", "/fw/liobox2_v900.lbf", "sha256:" + "a" * 64)
    rm.update_checksum()
    rm.save()

    rm2 = CompatibilityRegistryManager(rm.path)
    rm2.load()
    assert "9.0.0" in rm2.data["firmware"]
    assert rm2.data["checksum"] == rm.data["checksum"]
