"""Registry model: load, validate, edit, and save compatibility_registry.json."""

import json
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import Version

from .checksum import compute_registry_checksum


class CompatibilityRegistryManager:
    """Load, edit, and persist the compatibility registry."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4, ensure_ascii=False)
            f.write("\n")

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_firmware(self, version: str, path: str, checksum: str) -> None:
        firmware = self.data.setdefault("firmware", {})
        if version in firmware:
            raise ValueError(f"Firmware version {version!r} already exists in registry.")
        firmware[version] = {"version": version, "path": path, "checksum": checksum}

    def add_app_version(self, version: str, proto_reqs: dict) -> None:
        reqs = self.data.setdefault("protocol_requirements", {})
        if version in reqs:
            raise ValueError(f"App version {version!r} already exists in registry.")
        reqs[version] = proto_reqs

    def add_script(self, axis: str, version: str, path: str, checksum: str) -> None:
        """Add a script version to an axis's available_scripts section."""
        axes = self.data.setdefault("axes", {})
        if axis not in axes:
            axes[axis] = {
                "description": f"Script compatibility axis: {axis}",
                "pairs": {},
                "available_scripts": {},
            }
        scripts = axes[axis].setdefault("available_scripts", {})
        if version in scripts:
            raise ValueError(f"Script version {version!r} already exists in axis {axis!r}.")
        scripts[version] = {"version": version, "path": path, "checksum": checksum}

    def add_pair(self, axis: str, left: str, right: str) -> None:
        axes = self.data.setdefault("axes", {})
        if axis not in axes:
            axes[axis] = {"description": f"Axis: {axis}", "pairs": {}}
        pairs = axes[axis].setdefault("pairs", {})
        if left not in pairs:
            pairs[left] = []
        if right not in pairs[left]:
            pairs[left].append(right)

    def remove_pair(self, axis: str, left: str, right: str) -> None:
        try:
            pairs = self.data["axes"][axis]["pairs"]
        except KeyError:
            raise ValueError(f"Axis {axis!r} not found.")
        if left not in pairs or right not in pairs[left]:
            raise ValueError(f"Pair {left!r} / {right!r} not found in axis {axis!r}.")
        pairs[left].remove(right)
        if not pairs[left]:
            del pairs[left]

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def bump_schema_version(self) -> str:
        """Increment the patch component of schemaVersion and return the new version string."""
        current = self.data.get("schemaVersion", "0.0.0")
        v = Version(current)
        new_version = f"{v.major}.{v.minor}.{v.micro + 1}"
        self.data["schemaVersion"] = new_version
        return new_version

    def update_checksum(self) -> str:
        checksum = compute_registry_checksum(self.data)
        self.data["checksum"] = checksum
        return checksum

    def update_generated_at(self) -> None:
        self.data["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
