"""Validation of provider-supplied definitions and of tool arguments.

Schemas come from servers we do not trust. Two consequences:
- References must stay inside the schema. jsonschema's default registry can fetch remote
  `$ref` URLs, which would let a server make this process issue requests, so an empty registry
  is always passed and non-local references are rejected at discovery.
- Validation errors report the field and the failed keyword only. jsonschema's messages quote
  the submitted value, which may be sensitive.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from opsassist.errors import ValidationFailed
from opsassist.tools.base import ToolDescriptor

TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")
_MAX_DESCRIPTION_CHARS = 4000


class InvalidDefinition(ValueError):
    """A provider's tool definition cannot be registered. The message is ours, not the server's."""


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def check_definition(descriptor: ToolDescriptor, *, max_schema_bytes: int) -> None:
    if not TOOL_NAME.fullmatch(descriptor.name):
        raise InvalidDefinition("Tool name has invalid characters or length")
    if len(descriptor.description) > _MAX_DESCRIPTION_CHARS:
        raise InvalidDefinition("Description is too long")
    schema = descriptor.input_schema
    if len(json.dumps(schema)) > max_schema_bytes:
        raise InvalidDefinition("Input schema is too large")
    if schema.get("type") != "object":
        raise InvalidDefinition("Input schema must describe an object")
    for node in _walk(schema):
        for keyword in _REFERENCE_KEYWORDS:
            reference = node.get(keyword)
            if isinstance(reference, str) and not reference.startswith("#"):
                raise InvalidDefinition("Input schema has a non-local reference")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidDefinition("Input schema is not valid JSON Schema") from exc


def validate_arguments(
    schema: dict[str, Any], arguments: dict[str, Any], *, max_bytes: int
) -> None:
    """Raise ValidationFailed unless the arguments satisfy the pinned schema.

    Unknown top-level fields are refused unless the schema defines a schema for them. A server
    declaring `additionalProperties: true` does not get to widen what reaches it.
    """
    if len(json.dumps(arguments)) > max_bytes:
        raise ValidationFailed(f"Tool arguments exceed the {max_bytes} byte limit")
    strict = dict(schema)
    if strict.get("additionalProperties", True) is True:
        strict["additionalProperties"] = False
    validator = Draft202012Validator(strict, registry=Registry())
    try:
        errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.path))
    except Unresolvable as exc:
        raise ValidationFailed("The tool's input schema cannot be resolved") from exc
    if errors:
        issues = [
            {
                "field": ".".join(str(part) for part in error.absolute_path) or "(root)",
                "issue": str(error.validator),
            }
            for error in errors[:20]
        ]
        raise ValidationFailed("Tool arguments do not match the schema", details={"issues": issues})
