"""Click-based CLI entry point for gooroo-registry."""

import shutil
import sys
from pathlib import Path

import click

from .checksum import compute_file_checksum
from .publisher import S3Publisher
from .registry import CompatibilityRegistryManager
from .validators import Severity, ValidationIssue, validate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_registry(ctx: click.Context) -> CompatibilityRegistryManager:
    registry_path = Path(ctx.obj["registry_path"])
    if not registry_path.exists():
        raise click.ClickException(f"Registry not found: {registry_path}")
    rm = CompatibilityRegistryManager(registry_path)
    rm.load()
    return rm


def _artifacts_dir(ctx: click.Context) -> Path:
    # The registry lives at  <root>/registry/compatibility_registry.json
    # so artifacts are at    <root>/artifacts/
    registry_path = Path(ctx.obj["registry_path"])
    return registry_path.parent.parent / "artifacts"


def _print_issues(issues: list[ValidationIssue]) -> None:
    for issue in issues:
        prefix = "✗ [error]" if issue.severity == Severity.ERROR else "⚠ [warn] "
        click.echo(f"  {prefix} {issue.message}")


def _bump_save(rm: CompatibilityRegistryManager) -> tuple[str, str]:
    """Refresh checksum + timestamp, persist.  Returns (old_ver, new_ver)."""
    old_ver = rm.data.get("schemaVersion", "?")
    new_ver = old_ver
    rm.update_generated_at()
    rm.update_checksum()
    rm.save()
    return old_ver, new_ver


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--registry",
    default="registry/compatibility_registry.json",
    show_default=True,
    help="Path to the compatibility registry JSON file.",
)
@click.pass_context
def cli(ctx: click.Context, registry: str) -> None:
    """Gooroo Releases — Compatibility Registry Manager."""
    ctx.ensure_object(dict)
    ctx.obj["registry_path"] = registry


@cli.command("increment-version")
@click.pass_context
def increment_version(ctx: click.Context) -> None:
    """Manually increment the registry schemaVersion."""
    rm = _load_registry(ctx)
    old_ver = rm.data.get("schemaVersion", "?")
    new_ver = rm.bump_schema_version()
    rm.update_generated_at()
    rm.update_checksum()
    rm.save()
    click.echo(f"Incremented schemaVersion: {old_ver} → {new_ver}")
    click.echo("Registry saved.")


# ---------------------------------------------------------------------------
# add-firmware
# ---------------------------------------------------------------------------


@cli.command("add-firmware")
@click.argument("version")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the .lbf firmware binary.",
)
@click.option("--s3-path", default=None, help="Override S3 object path (default: /firmware/Liobox2/<filename>).")
@click.pass_context
def add_firmware(ctx: click.Context, version: str, file_path: str, s3_path: str | None) -> None:
    """Add a firmware version with its .lbf binary."""
    rm = _load_registry(ctx)
    src = Path(file_path)

    if s3_path is None:
        s3_path = f"/firmware/Liobox2/{src.name}"

    click.echo(f"Computing SHA256 of {src.name}...")
    checksum = compute_file_checksum(src)
    click.echo(f"  {checksum}\n")

    rm.add_firmware(version, s3_path, checksum)

    # Copy binary into artifacts/
    dest = _artifacts_dir(ctx) / "firmware" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
        click.echo(f"Copied {src.name} → {dest}")

    # Warn about missing pairs
    paired_fw = {
        fw
        for fw_list in rm.data.get("axes", {}).get("app_firmware", {}).get("pairs", {}).values()
        for fw in fw_list
    }
    if version not in paired_fw:
        click.echo(
            f"\n⚠  Warning: Firmware {version} is not paired with any app version.\n"
            f"   Run: gooroo-registry add-pair app_firmware <app_version> {version}"
        )

    old_ver, new_ver = _bump_save(rm)
    click.echo("Checksum and timestamp updated.")
    click.echo("Registry saved.")


# ---------------------------------------------------------------------------
# add-app
# ---------------------------------------------------------------------------


@cli.command("add-app")
@click.argument("version")
@click.option("--gprot", required=True, help="gprotocol_version")
@click.option("--datamodel", required=True, help="device_datamodel_version")
@click.option("--std-cmd", required=True, help="gprotocol_std_command_set_version")
@click.option("--dev-cmd", required=True, help="gprotocol_dev_command_set_version")
@click.pass_context
def add_app(
    ctx: click.Context,
    version: str,
    gprot: str,
    datamodel: str,
    std_cmd: str,
    dev_cmd: str,
) -> None:
    """Add an app version with its protocol requirements."""
    rm = _load_registry(ctx)
    rm.add_app_version(
        version,
        {
            "gprotocol_version": gprot,
            "device_datamodel_version": datamodel,
            "gprotocol_std_command_set_version": std_cmd,
            "gprotocol_dev_command_set_version": dev_cmd,
        },
    )
    old_ver, new_ver = _bump_save(rm)
    click.echo(f"Added app version {version}.")
    click.echo("Registry saved.")


# ---------------------------------------------------------------------------
# add-script
# ---------------------------------------------------------------------------


@cli.command("add-script")
@click.argument("daw", type=click.Choice(["ableton", "reaper"], case_sensitive=False))
@click.argument("version")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the .zip script archive.",
)
@click.option("--s3-path", default=None, help="Override S3 object path.")
@click.pass_context
def add_script(
    ctx: click.Context,
    daw: str,
    version: str,
    file_path: str,
    s3_path: str | None,
) -> None:
    """Add a DAW script version with its .zip archive."""
    rm = _load_registry(ctx)
    src = Path(file_path)
    daw = daw.lower()
    axis = f"firmware_{daw}_script"

    if s3_path is None:
        s3_path = f"/scripts/{daw.capitalize()}/{src.name}"

    click.echo(f"Computing SHA256 of {src.name}...")
    checksum = compute_file_checksum(src)
    click.echo(f"  {checksum}\n")

    rm.add_script(axis, version, s3_path, checksum)

    dest = _artifacts_dir(ctx) / "scripts" / daw / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
        click.echo(f"Copied {src.name} → {dest}")

    paired_scripts = {
        s
        for s_list in rm.data.get("axes", {}).get(axis, {}).get("pairs", {}).values()
        for s in s_list
    }
    if version not in paired_scripts:
        click.echo(
            f"\n⚠  Warning: Script {version} is not paired with any firmware in {axis}.\n"
            f"   Run: gooroo-registry add-pair {axis} <fw_version> {version}"
        )

    old_ver, new_ver = _bump_save(rm)
    click.echo("Checksum and timestamp updated.")
    click.echo("Registry saved.")


# ---------------------------------------------------------------------------
# add-pair / remove-pair
# ---------------------------------------------------------------------------


@cli.command("add-pair")
@click.argument("axis")
@click.argument("left_version")
@click.argument("right_version")
@click.pass_context
def add_pair(ctx: click.Context, axis: str, left_version: str, right_version: str) -> None:
    """Add a compatibility pair to an axis."""
    rm = _load_registry(ctx)
    rm.add_pair(axis, left_version, right_version)
    old_ver, new_ver = _bump_save(rm)
    click.echo(f"Added pair [{axis}] {left_version} ↔ {right_version}")
    click.echo("Registry saved.")


@cli.command("remove-pair")
@click.argument("axis")
@click.argument("left_version")
@click.argument("right_version")
@click.pass_context
def remove_pair(ctx: click.Context, axis: str, left_version: str, right_version: str) -> None:
    """Remove a compatibility pair."""
    rm = _load_registry(ctx)
    rm.remove_pair(axis, left_version, right_version)
    old_ver, new_ver = _bump_save(rm)
    click.echo(f"Removed pair [{axis}] {left_version} ↔ {right_version}")
    click.echo("Registry saved.")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@cli.command("validate")
@click.option("--strict", is_flag=True, help="Treat warnings as errors.")
@click.option("--skip-artifacts", is_flag=True, help="Skip artifact file existence check.")
@click.pass_context
def validate(ctx: click.Context, strict: bool, skip_artifacts: bool) -> None:
    """Run all validators against the current registry."""
    rm = _load_registry(ctx)
    artifacts_dir = None if skip_artifacts else _artifacts_dir(ctx)

    issues = validate_all(rm.data, artifacts_dir=artifacts_dir)
    errors = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]

    if not issues:
        click.echo("✓ All checks passed.")
        return

    _print_issues(issues)
    click.echo(f"\n{len(errors)} error(s), {len(warnings)} warning(s).")

    if errors or (strict and warnings):
        sys.exit(1)


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


@cli.command("publish")
@click.option("--dry-run", is_flag=True, help="Show what would be uploaded without actually uploading.")
@click.option("--strict", is_flag=True, help="Refuse if there are any validation warnings.")
@click.option("--skip-validate", is_flag=True, help="Skip pre-publish validation (not recommended).")
@click.pass_context
def publish(ctx: click.Context, dry_run: bool, strict: bool, skip_validate: bool) -> None:
    """Publish registry and new artifacts to Infomaniak S3."""
    rm = _load_registry(ctx)
    artifacts_dir = _artifacts_dir(ctx)

    if not skip_validate:
        click.echo("Validating registry...")
        issues = validate_all(rm.data, artifacts_dir=artifacts_dir, allow_missing_artifacts=True)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        warnings = [i for i in issues if i.severity == Severity.WARNING]

        if errors:
            click.echo(f"  ✗ {len(errors)} error(s) — cannot publish:")
            _print_issues(errors)
            sys.exit(1)

        if warnings:
            click.echo(f"  ⚠ {len(warnings)} warning(s):")
            _print_issues(warnings)
            if strict:
                click.echo("  ✗ Refusing publish in --strict mode (warnings present).")
                sys.exit(1)

        click.echo(f"  ✓ Validation passed  ({len(errors)} error(s), {len(warnings)} warning(s))")

    publisher = S3Publisher(dry_run=dry_run)
    fw_folder = artifacts_dir / "firmware"
    scripts_dir = artifacts_dir / "scripts"

    # ── Step 1: fetch remote listing (one API call) ──────────────────────
    if dry_run:
        click.echo("\n[dry-run] Skipping remote check — treating all local files as new.")
        remote_set: set[str] = set()
    else:
        click.echo("\nFetching remote object listing…")
        try:
            remote_set = S3Publisher(dry_run=False).get_remote_set()
            click.echo(f"  {len(remote_set)} object(s) found on remote.")
        except Exception as exc:
            click.echo(f"  ✗ Could not reach remote: {exc}", err=True)
            sys.exit(1)

    # ── Step 2: build plan ───────────────────────────────────────────────
    click.echo("Scanning local folders…")
    plan = publisher.build_plan(
        rm,
        remote_set=remote_set,
        fw_folder=fw_folder,
        ableton_scripts_folder=scripts_dir,
        reaper_scripts_folder=scripts_dir,
    )

    # ── Step 3: print diff ───────────────────────────────────────────────
    if plan.to_upload:
        click.echo(f"\nTo upload ({len(plan.to_upload)} file(s)) + registry:")
        for _, remote_path, label in plan.to_upload:
            click.echo(f"  • {label}  →  {remote_path}")
    else:
        click.echo("\nNo new local files to upload.")

    if plan.already_remote:
        click.echo(f"\nAlready on S3 — will be skipped ({len(plan.already_remote)}):")
        for label in plan.already_remote:
            click.echo(f"  ⊘ {label}")

    if plan.missing:
        click.echo(f"\n⚠ Missing — neither local nor remote ({len(plan.missing)}):")
        for label in plan.missing:
            click.echo(f"  ✗ {label}")

    if not plan.to_upload:
        if dry_run:
            click.echo("\n(dry-run complete — nothing to upload)")
        return

    # ── Step 4: execute ──────────────────────────────────────────────────
    click.echo("")
    publisher.execute_plan(plan)

    if dry_run:
        click.echo("\n(dry-run complete — nothing was uploaded)")
    else:
        click.echo("\n✓ Publish complete!")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@cli.command("sync")
@click.option(
    "--target",
    required=True,
    type=click.Path(file_okay=False),
    help="Target directory to copy the registry into.",
)
@click.pass_context
def sync(ctx: click.Context, target: str) -> None:
    """Copy the registry to a GoorooLink assets directory for embedding."""
    src = Path(ctx.obj["registry_path"])
    if not src.exists():
        raise click.ClickException(f"Registry not found: {src}")
    target_dir = Path(target)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "compatibility_registry.json"
    shutil.copy2(src, dest)
    click.echo(f"Copied {src} → {dest}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show a summary of the current registry state."""
    rm = _load_registry(ctx)
    data = rm.data

    click.echo(f"Registry:      {ctx.obj['registry_path']}")
    click.echo(f"schemaVersion: {data.get('schemaVersion', 'n/a')}")
    click.echo(f"generatedAt:   {data.get('generatedAt', 'n/a')}")
    click.echo(f"checksum:      {data.get('checksum', 'n/a')}")
    click.echo()

    app_versions = list(data.get("protocol_requirements", {}).keys())
    fw_versions = list(data.get("firmware", {}).keys())
    click.echo(f"App versions  ({len(app_versions)}): {', '.join(app_versions) or '—'}")
    click.echo(f"Firmware      ({len(fw_versions)}): {', '.join(fw_versions) or '—'}")
    click.echo()

    for axis_name, axis_data in data.get("axes", {}).items():
        pairs = axis_data.get("pairs", {})
        total_pairs = sum(len(v) for v in pairs.values())
        scripts = axis_data.get("available_scripts", {})
        script_info = f", {len(scripts)} available script(s)" if scripts else ""
        click.echo(f"Axis [{axis_name}]: {total_pairs} pair(s){script_info}")


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


@cli.command("diff")
@click.pass_context
def diff(ctx: click.Context) -> None:
    """Show the diff between the local registry and the cloud registry."""
    rm = _load_registry(ctx)
    publisher = S3Publisher(dry_run=False)
    try:
        changes = publisher.diff_with_remote(rm)
        if changes:
            click.echo("Differences with remote:")
            for c in changes:
                click.echo(f"  {c}")
        else:
            click.echo("No differences.")
    except Exception as exc:
        click.echo(f"Could not connect to S3: {exc}", err=True)
        sys.exit(1)
