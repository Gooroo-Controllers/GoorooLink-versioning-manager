"""JSON schema definition for compatibility_registry.json."""

import jsonschema

# Reusable sub-schemas
_SEMVER_PATTERN = r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$"
_CHECKSUM_PATTERN = r"^sha256:[0-9a-f]{64}$"

_ARTIFACT_ENTRY = {
    "type": "object",
    "required": ["version", "path", "checksum"],
    "properties": {
        "version": {"type": "string"},
        "path": {"type": "string"},
        "checksum": {"type": "string", "pattern": _CHECKSUM_PATTERN},
    },
    "additionalProperties": False,
}

_SCRIPT_AXIS = {
    "type": "object",
    "required": ["description", "pairs"],
    "properties": {
        "description": {"type": "string"},
        "pairs": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "available_scripts": {
            "type": "object",
            "additionalProperties": _ARTIFACT_ENTRY,
        },
    },
    "additionalProperties": False,
}

_APP_FIRMWARE_AXIS = {
    "type": "object",
    "required": ["description", "pairs"],
    "properties": {
        "description": {"type": "string"},
        "pairs": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
    "additionalProperties": False,
}

REGISTRY_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "schemaVersion",
        "generatedAt",
        "protocol_requirements",
        "firmware",
        "axes",
        "checksum",
    ],
    "properties": {
        "schemaVersion": {"type": "string", "pattern": _SEMVER_PATTERN},
        "generatedAt": {"type": "string"},
        "checksum": {"type": "string", "pattern": _CHECKSUM_PATTERN},
        "protocol_requirements": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": [
                    "gprotocol_version",
                    "device_datamodel_version",
                    "gprotocol_std_command_set_version",
                    "gprotocol_dev_command_set_version",
                ],
                "properties": {
                    "gprotocol_version": {"type": "string"},
                    "device_datamodel_version": {"type": "string"},
                    "gprotocol_std_command_set_version": {"type": "string"},
                    "gprotocol_dev_command_set_version": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "firmware": {
            "type": "object",
            "additionalProperties": _ARTIFACT_ENTRY,
        },
        "axes": {
            "type": "object",
            "properties": {
                "app_firmware": _APP_FIRMWARE_AXIS,
            },
            "additionalProperties": _SCRIPT_AXIS,
        },
    },
    "additionalProperties": False,
}


def validate_schema(data: dict) -> None:
    """Validate registry data against the JSON schema. Raises jsonschema.ValidationError on failure."""
    jsonschema.validate(data, REGISTRY_SCHEMA)
