# gooroo-releases

Compatibility Registry Manager for the Gooroo ecosystem.

`gooroo-registry` is the single source of truth for cross-component version compatibility: App ↔ Firmware, Firmware ↔ Ableton Script, Firmware ↔ Reaper Script.

---

## Quick Start

```bash
# Python 3.13 from python.org ships with tkinter (required for the GUI).
# Homebrew Python 3.11 works for the CLI only.

# 1. Create and activate a virtual environment
/usr/local/bin/python3.13 -m venv .venv && source .venv/bin/activate

# 2. Install the CLI + GUI (editable)
pip install -e ".[dev]"

# 3. Launch the GUI
gooroo-registry-gui

# 4. Or use the CLI directly
gooroo-registry status
gooroo-registry validate --skip-artifacts
```

---

## Credential Setup (for publishing)

```bash
cp config/openstack_env.sh config/openstack_env.local.sh
# Edit openstack_env.local.sh with your Infomaniak OpenStack credentials
source config/openstack_env.local.sh
```

---

## Common Workflows

### New Firmware Release

```bash
gooroo-registry add-firmware 6.1.7 --file ./liobox2_v617.lbf
gooroo-registry add-pair app_firmware 0.2.5-beta 6.1.7
gooroo-registry add-pair firmware_ableton_script 6.1.7 3.1.6
gooroo-registry add-pair firmware_reaper_script 6.1.7 1.1.0
gooroo-registry validate --strict
gooroo-registry publish --strict
git commit -am "Add firmware 6.1.7" && git push
```

### New App Version Release

```bash
gooroo-registry add-app 0.2.6-beta --gprot 3.0.0 --datamodel 1.0.0 --std-cmd 1.0.0 --dev-cmd 2.0.0
gooroo-registry add-pair app_firmware 0.2.6-beta 6.1.6
gooroo-registry validate --strict && gooroo-registry publish --strict
git commit -am "Add app version 0.2.6-beta" && git push
gooroo-registry sync --target ~/Dev/GoorooLink/GoorooLinkContent/assets/
```

### New Ableton Script Release

```bash
gooroo-registry add-script ableton 3.2.0 --file ./Liobox2_AbletonScripts_3.2.0.zip
gooroo-registry add-pair firmware_ableton_script 6.1.6 3.2.0
gooroo-registry add-pair firmware_ableton_script 6.1.7 3.2.0
gooroo-registry validate --strict && gooroo-registry publish --strict
git commit -am "Add Ableton script 3.2.0" && git push
```

---

## CLI Reference

| Command | Description |
|---|---|
| `add-firmware <version> --file <path>` | Add a firmware version + upload binary |
| `add-app <version> --gprot ... --datamodel ... --std-cmd ... --dev-cmd ...` | Add app version + protocol requirements |
| `add-script <daw> <version> --file <path>` | Add a DAW script version + archive |
| `add-pair <axis> <left> <right>` | Add a compatibility pair |
| `remove-pair <axis> <left> <right>` | Remove a compatibility pair |
| `validate [--strict] [--skip-artifacts]` | Run all validators |
| `publish [--dry-run] [--strict]` | Publish to Infomaniak S3 |
| `sync --target <dir>` | Copy registry to GoorooLink assets |
| `status` | Show registry summary |
| `diff` | Show diff vs. cloud registry |

Pass `--registry <path>` to any command to target a non-default registry file.

---

## Repository Structure

```
registry/
  compatibility_registry.json   ← canonical source of truth
artifacts/
  firmware/                     ← .lbf binaries (Git LFS)
  scripts/
    ableton/                    ← .zip archives (Git LFS)
    reaper/
src/
  gooroo_registry/
    cli.py                      ← CLI entry point
    registry.py                 ← Registry model
    checksum.py                 ← SHA-256 utilities
    validators.py               ← Completeness + integrity checks
    publisher.py                ← Infomaniak S3 upload
    sparkle_generator.py        ← Sparkle appcast XML
    schema.py                   ← JSON schema
tests/
config/
  openstack_env.sh              ← Credential template (do not commit real values)
```

---

## Running Tests

```bash
pytest
pytest --cov=gooroo_registry --cov-report=term-missing
```
