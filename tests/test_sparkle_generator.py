from pathlib import Path

import pytest

from gooroo_registry.sparkle_generator import generate_appcast


def test_generate_returns_valid_xml():
    xml = generate_appcast(
        app_title="GoorooLink",
        app_version="0.2.6",
        download_url="https://example.com/GoorooLink-0.2.6.dmg",
        release_notes_url="https://example.com/release-notes/0.2.6.html",
        file_size=10_000_000,
    )
    assert "<?xml" in xml
    assert "<rss" in xml
    assert "GoorooLink" in xml
    assert "0.2.6" in xml


def test_generate_includes_sparkle_namespaces():
    xml = generate_appcast(
        app_title="GoorooLink",
        app_version="0.2.6",
        download_url="https://example.com/GoorooLink-0.2.6.dmg",
        release_notes_url="https://example.com/rn.html",
        file_size=5_000_000,
    )
    assert "andymatuschak.org" in xml
    assert "sparkle:version" in xml


def test_generate_includes_enclosure_with_file_size():
    xml = generate_appcast(
        app_title="GoorooLink",
        app_version="0.2.6",
        download_url="https://example.com/GoorooLink-0.2.6.dmg",
        release_notes_url="https://example.com/rn.html",
        file_size=7_654_321,
    )
    assert "enclosure" in xml
    assert "7654321" in xml


def test_generate_writes_to_file(tmp_path):
    out = tmp_path / "appcast.xml"
    generate_appcast(
        app_title="GoorooLink",
        app_version="0.2.6",
        download_url="https://example.com/GoorooLink-0.2.6.dmg",
        release_notes_url="https://example.com/rn.html",
        file_size=1_000,
        output_path=out,
    )
    assert out.exists()
    content = out.read_text()
    assert "GoorooLink" in content


def test_min_system_version_default():
    xml = generate_appcast(
        app_title="GoorooLink",
        app_version="0.2.6",
        download_url="https://example.com/GoorooLink-0.2.6.dmg",
        release_notes_url="https://example.com/rn.html",
        file_size=1_000,
    )
    assert "11.0" in xml


def test_min_system_version_override():
    xml = generate_appcast(
        app_title="GoorooLink",
        app_version="0.2.6",
        download_url="https://example.com/GoorooLink-0.2.6.dmg",
        release_notes_url="https://example.com/rn.html",
        file_size=1_000,
        min_system_version="12.0",
    )
    assert "12.0" in xml
