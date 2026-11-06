"""Shared, data-driven Live Object Model validation without importing Live.

The catalogue records documentation, not a claim that a particular Live build
supports a member. Runtime transports must still check the actual object/API.
"""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any


class LomValidationError(ValueError):
    """A request is outside the documented, bounded LOM contract."""


class LomCatalog:
    MAX_PATH_TOKENS = 64
    MAX_JSON_DEPTH = 16
    MAX_JSON_ITEMS = 8192
    MAX_JSON_BYTES = 1024 * 1024
    MAX_STRING_LENGTH = 65536
    MIN_NATIVE_OBJECT_ID = -2147483648
    MAX_NATIVE_OBJECT_ID = 2147483647
    ROOT_TYPES = {
        "live_set": "Song",
        "live_app": "Application",
        "control_surfaces": "ControlSurface",
        "this_device": "Device",
    }

    def __init__(self, schema_path: str | Path | None = None):
        path = (
            Path(schema_path)
            if schema_path is not None
            else Path(__file__).with_name("lom_schema.json")
        )
        self.schema = json.loads(path.read_text(encoding="utf-8-sig"))
        self.objects = self.schema["objects"]
        if not isinstance(self.objects, dict) or not self.objects:
            raise LomValidationError("LOM schema must contain object definitions")

    def object_definition(self, name: str) -> dict:
        if not isinstance(name, str) or name not in self.objects:
            raise LomValidationError(f"Unknown documented LOM object type: {name!r}")
        return copy.deepcopy(self.objects[name])

    get_type = object_definition

    def is_a(self, actual_type: str, declared_type: str) -> bool:
        if not isinstance(actual_type, str) or not isinstance(declared_type, str):
            return False
        seen = set()
        current = actual_type
        while current in self.objects and current not in seen:
            if current == declared_type:
                return True
            seen.add(current)
            current = self.objects[current].get("parent")
        return False

    def resolve_member(self, object_type: str, member: str) -> dict:
        self.object_definition(object_type)
        explicit_string_method = member == "__str__" and self.is_a(object_type, "DeviceParameter")
        if not isinstance(member, str) or (
            not explicit_string_method and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", member)
        ):
            raise LomValidationError("Member must be an exact documented identifier")
        current, seen = object_type, set()
        while current is not None and current not in seen:
            seen.add(current)
            definition = self.objects[current]
            if member in definition["members"]:
                result = copy.deepcopy(definition["members"][member])
                result["declaring_type"] = current
                return result
            current = definition.get("parent")
        raise LomValidationError(f"Undocumented member: {object_type}.{member}")

    def get_member(self, object_type: str, member: str, kind: str | None = None) -> dict:
        result = self.resolve_member(object_type, member)
        if kind is not None and result["kind"] != kind:
            raise LomValidationError("Member kind does not match the requested operation")
        return result

    def validate_operation(self, object_type: str, member: str, operation: str) -> dict:
        if operation not in {"get", "set", "call", "observe"}:
            raise LomValidationError("operation must be get, set, call, or observe")
        definition = self.resolve_member(object_type, member)
        if operation not in definition["access"]:
            raise LomValidationError(f"{object_type}.{member} does not document {operation} access")
        return definition

    def _expanded_types(self, types: set[str]) -> set[str]:
        return {actual for actual in self.objects if any(self.is_a(actual, base) for base in types)}

    def validate_path(self, path: str) -> str:
        if not isinstance(path, str) or len(path) > 4096:
            raise LomValidationError("LOM path must be a bounded string")
        tokens = path.split()
        if len(tokens) == 2 and tokens[0] == "id":
            token = tokens[1]
            if token == "0":
                raise LomValidationError("Native ID zero is a null value, not a valid target")
            decimal = re.fullmatch(r"-?[1-9][0-9]{0,9}", token)
            opaque = re.fullmatch(
                r"(?:python:[A-Za-z0-9_-]{1,128}:)?(?:[0-9a-fA-F]{32}|[0-9a-fA-F-]{36})", token
            )
            if not decimal and not opaque:
                raise LomValidationError("Invalid bounded native/scoped LOM object ID")
            if decimal and not self.MIN_NATIVE_OBJECT_ID <= int(token) <= self.MAX_NATIVE_OBJECT_ID:
                raise LomValidationError("Native object ID is outside signed 32-bit bounds")
            return " ".join(tokens)
        if not tokens or len(tokens) > self.MAX_PATH_TOKENS or tokens[0] not in self.ROOT_TYPES:
            raise LomValidationError("Invalid LOM root or path token count")
        if any("__" in token for token in tokens):
            raise LomValidationError("Dunder names are forbidden in LOM paths")
        current = self._expanded_types({self.ROOT_TYPES[tokens[0]]})
        index, collection = 1, tokens[0] == "control_surfaces"
        while index < len(tokens):
            token = tokens[index]
            if collection:
                if not re.fullmatch(r"0|[1-9][0-9]*", token) or len(token) > 10:
                    raise LomValidationError("A collection child requires a nonnegative index")
                if int(token) > 2147483647:
                    raise LomValidationError("LOM index exceeds the supported integer range")
                index += 1
                collection = False
                continue
            if token == "canonical_parent":
                # This is a documented LOM navigation relationship. Its actual
                # type depends on the runtime container; it is not getattr-any.
                current = set(self.objects)
                index += 1
                continue
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token):
                raise LomValidationError("LOM paths contain child identifiers and indexes only")
            candidates = []
            for object_type in current:
                try:
                    member = self.resolve_member(object_type, token)
                except LomValidationError:
                    continue
                if member["kind"] == "children" and "get" in member["access"]:
                    candidates.append(member)
            if not candidates:
                raise LomValidationError(f"Undocumented child in LOM path: {token}")
            target_types = {value for member in candidates for value in member["ref_types"]}
            if not target_types:
                raise LomValidationError("Child type is not documented; cannot navigate it safely")
            collection = any(member.get("collection", False) for member in candidates)
            current = self._expanded_types(target_types)
            index += 1
        return " ".join(tokens)

    def _bounded_json(self, value: Any) -> Any:
        count = 0
        byte_count = 0

        def visit(item: Any, depth: int) -> Any:
            nonlocal count, byte_count
            count += 1
            byte_count += len(item.encode("utf-8")) if isinstance(item, str) else 8
            if count > self.MAX_JSON_ITEMS or depth > self.MAX_JSON_DEPTH:
                raise LomValidationError("JSON value exceeds catalogue size/depth limits")
            if byte_count > self.MAX_JSON_BYTES:
                raise LomValidationError("JSON value exceeds the catalogue byte budget")
            if item is None or isinstance(item, bool):
                return item
            if isinstance(item, int):
                if abs(item) > 9007199254740991:
                    raise LomValidationError("Integer is outside the exact JSON transport range")
                return item
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise LomValidationError("Numeric values must be finite")
                return item
            if isinstance(item, str):
                if len(item) > self.MAX_STRING_LENGTH or "\x00" in item:
                    raise LomValidationError("String exceeds transport bounds or contains NUL")
                return item
            if isinstance(item, list):
                return [visit(child, depth + 1) for child in item]
            if isinstance(item, dict):
                if any(not isinstance(key, str) or len(key) > 256 for key in item):
                    raise LomValidationError("Dictionary keys must be bounded strings")
                byte_count += sum(len(key.encode("utf-8")) for key in item)
                if byte_count > self.MAX_JSON_BYTES:
                    raise LomValidationError("Dictionary exceeds the catalogue byte budget")
                return {key: visit(child, depth + 1) for key, child in item.items()}
            raise LomValidationError("Value must be JSON data, not a Python/native object")

        return visit(value, 0)

    def _validate_reference(self, descriptor: dict, value: Any) -> dict | None:
        def null_reference():
            if not descriptor.get("nullable", False):
                raise LomValidationError("This documented reference argument is not nullable")
            return None

        if value is None:
            return null_reference()
        if isinstance(value, dict) and "$live_id" in value:
            if set(value) - {"$live_id", "identity", "object_type"}:
                raise LomValidationError("Unexpected native reference fields")
            token = value["$live_id"]
            if isinstance(token, bool) or not isinstance(token, (int, str)):
                raise LomValidationError("$live_id must be a bounded native/scoped identifier")
            if "identity" in value and (
                not isinstance(value["identity"], str)
                or not re.fullmatch(
                    r"(?:max|python):[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}", value["identity"]
                )
            ):
                raise LomValidationError("Native reference identity must be backend-scoped")
            actual_type = value.get("object_type")
            if actual_type is not None:
                self._validate_reference_type(descriptor, actual_type)
            if token == 0 or token == "0":
                return null_reference()
            self.validate_path("id " + str(token))
            # The transport must verify pool ownership, current scope, and actual
            # native type. A caller-provided object_type is never runtime proof.
            return dict(value)
        if not isinstance(value, dict) or set(value) != {"$ref", "object_type"}:
            raise LomValidationError("Object references require exactly $ref and object_type")
        actual_type = value["object_type"]
        self._validate_reference_type(descriptor, actual_type)
        if isinstance(value["$ref"], str) and value["$ref"].split() == ["id", "0"]:
            return null_reference()
        return {"$ref": self.validate_path(value["$ref"]), "object_type": actual_type}

    def _validate_reference_type(self, descriptor: dict, actual_type: Any) -> None:
        if not isinstance(actual_type, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.]{0,127}", actual_type
        ):
            raise LomValidationError("Reference object_type must be a bounded class identifier")
        allowed = descriptor.get("ref_types", [])
        if allowed and not any(self.is_a(actual_type, expected) for expected in allowed):
            raise LomValidationError("Reference object_type does not match the documented LOM type")

    def _validate_dictionary(self, schema: dict, value: Any) -> Any:
        if schema.get("native_schema_from_readback"):
            if not isinstance(value, dict):
                raise LomValidationError("Expected a bounded native-validated dictionary")
            # The official page omits its keys. A transport must compare the
            # current native readback shape when available and label the final
            # signature validation as native, rather than claiming formal types.
            return self._bounded_json(value)
        if "oneOf" in schema:
            matches = []
            for choice in schema["oneOf"]:
                try:
                    matches.append(self._validate_dictionary(choice, value))
                except LomValidationError:
                    pass
            if not matches:
                raise LomValidationError("Value matches none of the documented dictionary modes")
            return matches[0]
        if "$ref_types" in schema:
            return self._validate_reference(
                {"ref_types": schema["$ref_types"], "nullable": schema.get("nullable", False)},
                value,
            )
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                raise LomValidationError("Expected a dictionary")
            properties = schema.get("properties", {})
            if set(schema.get("required", [])) - set(value):
                raise LomValidationError("Required dictionary fields are missing")
            unknown = set(value) - set(properties)
            if unknown and not schema.get("additionalProperties", False):
                raise LomValidationError(f"Unknown dictionary fields: {sorted(unknown)}")
            return {
                key: self._validate_dictionary(properties[key], child)
                for key, child in value.items()
            }
        if kind == "array":
            if not isinstance(value, list):
                raise LomValidationError("Expected a list")
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 4096):
                raise LomValidationError("List length is outside documented transport bounds")
            return [self._validate_dictionary(schema.get("items", {}), child) for child in value]
        mapped = {"integer": "int", "number": "float", "string": "symbol", "boolean": "bool"}
        descriptor = {"value_type": mapped.get(kind, kind or "json_scalar")}
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "enum"):
            if key in schema:
                descriptor[key] = schema[key]
        return self._validate_typed(descriptor, value)

    def _validate_typed(self, descriptor: dict, value: Any) -> Any:
        kind = descriptor.get("value_type", "json_scalar")
        if value is None and descriptor.get("nullable", False):
            return None
        if kind in {"reference", "object"}:
            return self._validate_reference(descriptor, value)
        if kind in {"dictionary", "dict"}:
            if not descriptor.get("dictionary"):
                raise LomValidationError("A documented dictionary schema is required")
            return self._validate_dictionary(descriptor["dictionary"], value)
        if kind == "list":
            if not isinstance(value, list) or len(value) > descriptor.get("max_items", 4096):
                raise LomValidationError("Expected a bounded list")
            return [
                self._validate_typed(descriptor.get("items", {"value_type": "json_scalar"}), child)
                for child in value
            ]
        if kind in {"int", "integer"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind in {"float", "double", "number"}:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif kind in {"bool", "boolean"}:
            if isinstance(value, bool):
                valid = True
            elif isinstance(value, int) and value in (0, 1):
                value, valid = bool(value), True
            else:
                valid = False
        elif kind in {"symbol", "string"}:
            valid = isinstance(value, str)
        elif kind == "bang":
            valid = value is None
        elif kind in {"json_scalar", "native"}:
            if isinstance(value, dict) and (
                set(value) == {"$ref", "object_type"} or "$live_id" in value
            ):
                return self._validate_reference(descriptor, value)
            if isinstance(value, list):
                return [
                    self._validate_typed({"value_type": "json_scalar"}, child) for child in value
                ]
            valid = value is None or isinstance(value, (str, bool, int, float))
        else:
            raise LomValidationError(f"Unsupported documented value type: {kind}")
        if not valid:
            raise LomValidationError(f"Expected documented type {kind}")
        # Boolean documentation frequently explains only one side ("1 = on").
        # Its full domain remains false/true, including the documented 0/1
        # transport representation, even when loading an older schema fragment.
        if (
            kind not in {"bool", "boolean"}
            and "enum" in descriptor
            and value not in descriptor["enum"]
        ):
            raise LomValidationError("Value is outside the documented enumeration")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise LomValidationError("Numeric values must be finite")
            if "minimum" in descriptor and value < descriptor["minimum"]:
                raise LomValidationError("Value is below the documented minimum")
            if "maximum" in descriptor and value > descriptor["maximum"]:
                raise LomValidationError("Value is above the documented maximum")
            if "exclusiveMinimum" in descriptor and value <= descriptor["exclusiveMinimum"]:
                raise LomValidationError("Value must exceed the documented minimum")
            if "exclusiveMaximum" in descriptor and value >= descriptor["exclusiveMaximum"]:
                raise LomValidationError("Value must be below the documented maximum")
        return value

    def validate_value(self, memberdict: dict, value: Any) -> Any:
        return self._validate_typed(memberdict, self._bounded_json(value))

    def validate_arguments(self, memberdict: dict, arguments: list) -> list:
        if not isinstance(arguments, list) or len(arguments) > 4096:
            raise LomValidationError("Function arguments must be a bounded JSON list")
        values = self._bounded_json(arguments)
        if len(values) == 1 and isinstance(values[0], dict) and memberdict.get("dictionary"):
            return [self._validate_dictionary(memberdict["dictionary"], values[0])]
        signatures = memberdict.get("argument_modes") or [memberdict.get("arguments", [])]
        if memberdict.get("signature_validation") == "native_signature_validation":
            return [self._validate_typed({"value_type": "native"}, value) for value in values]
        for signature in signatures:
            minimum = sum(bool(arg.get("required", True)) for arg in signature)
            variadic = bool(signature and signature[-1].get("variadic", False))
            if len(values) < minimum or (not variadic and len(values) > len(signature)):
                continue
            try:
                return [
                    self._validate_typed(signature[min(index, len(signature) - 1)], value)
                    for index, value in enumerate(values)
                ]
            except LomValidationError:
                continue
        raise LomValidationError("Arguments do not match a documented function signature")

    def describe(
        self,
        object_type: str | None = None,
        member: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (offset, limit)):
            raise LomValidationError("offset and limit must be integers")
        if offset < 0 or not 1 <= limit <= 500:
            raise LomValidationError("offset must be nonnegative; limit must be 1..500")
        if member is not None:
            if object_type is None:
                raise LomValidationError("A member query requires an object_type")
            return self.resolve_member(object_type, member)
        if object_type is not None:
            definition = self.object_definition(object_type)
            names, current = {}, object_type
            while current is not None:
                for name in self.objects[current]["members"]:
                    names.setdefault(name, self.resolve_member(object_type, name))
                current = self.objects[current].get("parent")
            rows = [names[name] for name in sorted(names)]
            return {
                "object_type": object_type,
                "parent": definition["parent"],
                "total": len(rows),
                "members": rows[offset : offset + limit],
                "next_offset": offset + limit if offset + limit < len(rows) else None,
            }
        names = sorted(self.objects)
        return {
            "object_count": len(names),
            "member_count": sum(len(o["members"]) for o in self.objects.values()),
            "objects": names[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(names) else None,
            "documentation_live_version": self.schema.get("documentation_live_version"),
        }
