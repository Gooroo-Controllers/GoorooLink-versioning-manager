"""SHA-256 checksum utilities for the registry and artifact files."""

import hashlib
import json
from pathlib import Path


def compute_registry_checksum(data: dict) -> str:
    """Compute sha256 over sorted-keys compact JSON, excluding the 'checksum' field.

    This matches the algorithm used by the C++ CompatibilityRegistry class and
    the legacy compute_registry_checksum.py script.
    """
    payload = {k: v for k, v in data.items() if k != "checksum"}
    json_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(json_bytes).hexdigest()
    return f"sha256:{digest}"


def compute_file_checksum(path: Path) -> str:
    """Compute sha256 of a file, reading in 64 KB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"
