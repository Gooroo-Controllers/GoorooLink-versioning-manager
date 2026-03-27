"""Sparkle appcast XML generation."""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def generate_appcast(
    app_title: str,
    app_version: str,
    download_url: str,
    release_notes_url: str,
    file_size: int,
    min_system_version: str = "11.0",
    output_path: Optional[Path] = None,
) -> str:
    """Generate a Sparkle-compatible appcast XML string.

    Args:
        app_title:           Human-readable application name (e.g. "GoorooLink").
        app_version:         CFBundleShortVersionString (e.g. "0.2.6").
        download_url:        Direct HTTPS URL to the .dmg or .zip distribution.
        release_notes_url:   URL to the HTML release notes page.
        file_size:           Size of the downloadable file in bytes.
        min_system_version:  Minimum macOS version (default: "11.0").
        output_path:         If provided, write the resulting XML to this file.

    Returns:
        The appcast XML as a string (without XML declaration header).
    """
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle",
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        },
    )
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = app_title
    ET.SubElement(channel, "description").text = f"Most recent changes with links to updates."
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "link").text = release_notes_url

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Version {app_version}"
    ET.SubElement(item, "sparkle:releaseNotesLink").text = release_notes_url
    ET.SubElement(item, "pubDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    ET.SubElement(item, "sparkle:minimumSystemVersion").text = min_system_version
    ET.SubElement(item, "sparkle:version").text = app_version
    ET.SubElement(item, "sparkle:shortVersionString").text = app_version
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": download_url,
            "sparkle:version": app_version,
            "sparkle:shortVersionString": app_version,
            "length": str(file_size),
            "type": "application/octet-stream",
        },
    )

    xml_body = ET.tostring(rss, encoding="unicode")
    full_xml = f'<?xml version="1.0" encoding="utf-8"?>\n{xml_body}\n'

    if output_path is not None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(full_xml, encoding="utf-8")

    return full_xml
