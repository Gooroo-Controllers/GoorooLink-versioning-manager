import pytest

from gooroo_registry.checksum import compute_file_checksum, compute_registry_checksum


def test_checksum_excludes_checksum_field():
    data = {"schemaVersion": "1.0.0", "foo": "bar", "checksum": "sha256:" + "0" * 64}
    result = compute_registry_checksum(data)
    assert result.startswith("sha256:")
    assert len(result) == 71  # "sha256:" (7) + 64 hex chars


def test_checksum_is_deterministic():
    data = {"b": 2, "a": 1}
    assert compute_registry_checksum(data) == compute_registry_checksum(data)


def test_checksum_sort_keys_independent():
    """Key insertion order must not affect the checksum."""
    data1 = {"b": 2, "a": 1}
    data2 = {"a": 1, "b": 2}
    assert compute_registry_checksum(data1) == compute_registry_checksum(data2)


def test_checksum_matches_stored_value(valid_registry):
    """The stored checksum in the fixture must match what we compute."""
    stored = valid_registry["checksum"]
    computed = compute_registry_checksum(valid_registry)
    assert stored == computed


def test_checksum_changes_on_mutation(valid_registry):
    import copy
    data = copy.deepcopy(valid_registry)
    original = compute_registry_checksum(data)
    data["schemaVersion"] = "99.0.0"
    mutated = compute_registry_checksum(data)
    assert original != mutated


def test_compute_file_checksum(tmp_path):
    f = tmp_path / "test.bin"
    f.write_bytes(b"hello world")
    result = compute_file_checksum(f)
    assert result.startswith("sha256:")
    assert len(result) == 71
    # Known SHA-256 of "hello world"
    # Known SHA-256 of b"hello world"
    assert result == "sha256:b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    # Verify it's deterministic
    assert compute_file_checksum(f) == result
