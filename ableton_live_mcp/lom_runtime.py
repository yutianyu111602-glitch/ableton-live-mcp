"""Catalogue-bounded Live Python transport, usable without MCP or _Framework.

All calls, including listener cleanup, run on Live's main thread. The caller
must marshal requests onto that thread; the Remote Script does this already.
"""

import math
import re
import time
import uuid
import weakref
from collections import deque

from .lom_catalog import LomCatalog


class Unsupported(Exception):
    """The Python binding cannot establish this official operation safely."""


class LomRuntime:
    MAX_SUBSCRIPTIONS = 256
    MAX_OWNER_SUBSCRIPTIONS = 64
    MAX_AUTOMATIC_DETACH_RETRIES = 1
    CONTROL_SURFACE_BASE_MODULES = {
        "_Framework.ControlSurface",
        "ableton.v2.control_surface.control_surface",
    }
    DEVICE_CLASSES = {
        "PluginDevice": "PluginDevice",
        "AuPluginDevice": "PluginDevice",
        "InstrumentGroupDevice": "RackDevice",
        "AudioEffectGroupDevice": "RackDevice",
        "MidiEffectGroupDevice": "RackDevice",
        "DrumGroupDevice": "RackDevice",
        "OriginalSimpler": "SimplerDevice",
        "Simpler": "SimplerDevice",
        "MxDeviceAudioEffect": "MaxDevice",
        "MxDeviceInstrument": "MaxDevice",
        "MxDeviceMidiEffect": "MaxDevice",
        "Compressor2": "CompressorDevice",
        "Eq8": "Eq8Device",
        "Drift": "DriftDevice",
        "DrumCell": "DrumCellDevice",
        "HybridReverb": "HybridReverbDevice",
        "Looper": "LooperDevice",
        "Meld": "MeldDevice",
        "Roar": "RoarDevice",
        "Shifter": "ShifterDevice",
        "SpectralResonator": "SpectralResonatorDevice",
        "Wavetable": "WavetableDevice",
    }

    def __init__(self, song, application, control_surface, clock=time.monotonic, catalog=None):
        self.song = song
        self.application = application
        self.control_surface = control_surface
        self.clock = clock
        self.catalog = catalog if catalog is not None else LomCatalog()
        self.scope = uuid.uuid4().hex
        self.identities = []
        self.type_cache = {}
        self.subscriptions = {}
        self.tombstones = {}
        self.closed = False
        self.scheduled = False

    @staticmethod
    def _integer(value, name, low=0, high=None):
        if isinstance(value, bool) or not isinstance(value, int) or value < low:
            raise ValueError(name + " is outside its integer range")
        if high is not None and value > high:
            raise ValueError(name + " is outside its integer range")
        return value

    @staticmethod
    def _finite(value, name, low=None, high=None):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(name + " must be finite")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(name + " is outside its allowed range")
        return float(value)

    @staticmethod
    def _owner(payload):
        owner = payload.get("owner")
        if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", owner):
            raise ValueError("A nonempty, bounded owner token is required")
        return owner

    def _identity(self, obj):
        for previous, identity in self.identities:
            try:
                if obj is previous or obj == previous:
                    return identity
            except (RuntimeError, ReferenceError):
                continue
        identity = "python:" + self.scope + ":" + uuid.uuid4().hex
        self.identities.append((obj, identity))
        if len(self.identities) > 16384:
            self.identities = self.identities[-8192:]
        return identity

    def _known_type(self, name):
        if name in self.type_cache:
            return self.type_cache[name]
        try:
            self.catalog.object_definition(name)
            known = True
        except (KeyError, ValueError):
            known = False
        self.type_cache[name] = known
        return known

    def _id_object(self, token):
        for obj, identity in self.identities:
            if token == identity or token == identity.rsplit(":", 1)[1]:
                return obj
        raise Unsupported(
            "unknown_or_stale_live_id: this Python runtime has no such scoped reference"
        )

    def _actual_type(self, obj):
        if obj is self.song:
            return "Song"
        if obj is self.application:
            return "Application"
        if obj is self.control_surface:
            return "ControlSurface"
        try:
            classes = (
                obj.__class__.__mro__ if isinstance(obj, weakref.ProxyTypes) else type(obj).__mro__
            )
        except ReferenceError as error:
            raise Unsupported("The native object proxy is no longer valid") from error
        if any(
            cls.__name__ == "ControlSurface" and cls.__module__ in self.CONTROL_SURFACE_BASE_MODULES
            for cls in classes
        ):
            return "ControlSurface"
        candidates = []
        for cls in classes:
            name, module = cls.__name__, cls.__module__
            if name == "ControlSurface":
                # A caller-side or unrelated class with the same name is not proof.
                continue
            if name == "View":
                parts = module.split(".")
                for prefix in reversed(parts):
                    if self._known_type(prefix + ".View"):
                        candidates.append(prefix + ".View")
                        break
            if self._known_type(name):
                candidates.append(name)
        if not candidates:
            raise Unsupported(
                "object_type_unconfirmed: Python binding type is not in the official model"
            )
        actual = candidates[0]
        if self.catalog.is_a(actual, "Device"):
            class_name = getattr(obj, "class_name", None)
            actual = self.DEVICE_CLASSES.get(class_name, actual)
        return actual

    def _check_type(self, obj, declared):
        self.catalog.object_definition(declared)
        actual = self._actual_type(obj)
        if not self.catalog.is_a(actual, declared):
            raise Unsupported("object_type_mismatch: native " + actual + " is not " + declared)
        return actual

    def _root(self, tokens):
        root = tokens.pop(0)
        if root == "id":
            if not tokens:
                raise ValueError("An object ID is required")
            token = tokens.pop(0)
            obj = self._id_object(token)
            return obj, self._actual_type(obj), "id " + token
        if root == "live_set":
            return self.song, "Song", root
        if root == "live_app":
            return self.application, "Application", root
        if root == "this_device":
            raise Unsupported("use_max_backend: this_device has no Python Remote Script equivalent")
        if root == "control_surfaces":
            surfaces = getattr(self.application, "control_surfaces", None)
            if surfaces is None or not tokens or not tokens[0].isdigit():
                raise Unsupported(
                    "use_max_backend: the full ControlSurface root list is not exposed"
                )
            index = int(tokens.pop(0))
            if index >= len(surfaces):
                raise IndexError("ControlSurface index out of range")
            surface = surfaces[index]
            if isinstance(surface, weakref.ReferenceType):
                surface = surface()
                if surface is None:
                    raise Unsupported("The registered ControlSurface reference has expired")
            return surface, "ControlSurface", "control_surfaces " + str(index)
        raise ValueError("Unknown Live root")

    def _resolve(self, path, declared):
        path = self.catalog.validate_path(path)
        tokens = path.split()
        obj, current_type, consumed = self._root(tokens)
        self._check_type(obj, current_type)
        while tokens:
            name = tokens.pop(0)
            if name == "canonical_parent":
                obj = getattr(obj, "canonical_parent", None)
                if obj is None:
                    raise Unsupported("The canonical parent is not exposed by this Python object")
                consumed += " canonical_parent"
                current_type = self._actual_type(obj)
                continue
            member = self.catalog.resolve_member(current_type, name)
            if member.get("kind") != "children":
                raise ValueError("Paths may traverse only documented children: " + name)
            if "get" not in member.get("access", []):
                raise Unsupported("Child is not readable: " + name)
            try:
                obj = getattr(obj, name)
            except AttributeError as error:
                raise Unsupported(
                    "use_max_backend: child is missing from Python: " + name
                ) from error
            consumed += " " + name
            if member.get("collection"):
                if not tokens or not tokens[0].isdigit():
                    raise ValueError("A child collection requires an index: " + name)
                index = int(tokens.pop(0))
                if index >= len(obj):
                    raise IndexError("Child index out of range")
                obj = obj[index]
                consumed += " " + str(index)
            if obj is None:
                raise Unsupported("The requested child does not currently exist: " + consumed)
            actual = self._actual_type(obj)
            refs = member.get("ref_types") or (member.get("items") or {}).get("ref_types", [])
            if refs and not any(self.catalog.is_a(actual, ref) for ref in refs):
                raise Unsupported("object_type_mismatch at child " + consumed)
            current_type = actual
        self._check_type(obj, declared)
        target = {
            "path": path,
            "object_type": declared,
            "identity": self._identity(obj),
            "backend": "python",
            "actual_type": self._actual_type(obj),
        }
        return obj, target

    def _bounded_json(self, value, depth=0, budget=None):
        budget = [8192] if budget is None else budget
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("JSON item count limit exceeded")
        if depth > 12:
            raise ValueError("JSON nesting limit exceeded")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return self._finite(value, "JSON number")
        if isinstance(value, str):
            if len(value) > 65536:
                raise ValueError("JSON string is too long")
            return value
        if isinstance(value, list):
            if len(value) > 4096:
                raise ValueError("JSON array is too large")
            return [self._bounded_json(item, depth + 1, budget) for item in value]
        if isinstance(value, dict):
            if len(value) > 256 or any(not isinstance(key, str) for key in value):
                raise ValueError("Invalid JSON object")
            return {key: self._bounded_json(item, depth + 1, budget) for key, item in value.items()}
        raise ValueError("Only bounded JSON values are accepted")

    def _native_argument(self, value):
        if isinstance(value, dict):
            if "$live_id" in value and set(value) <= {"$live_id", "object_type", "identity"}:
                obj = self._id_object(value["$live_id"])
                self._check_type(obj, value["object_type"])
                if value.get("identity", self._identity(obj)) != self._identity(obj):
                    raise ValueError("stale_target: native reference identity changed")
                return obj
            if "$ref" not in value or not set(value) <= {"$ref", "object_type", "identity"}:
                raise Unsupported(
                    "use_max_backend: native dictionary arguments require a typed adapter"
                )
            obj, target = self._resolve(value["$ref"], value["object_type"])
            if value.get("identity", target["identity"]) != target["identity"]:
                raise ValueError("stale_target: native reference identity changed")
            return obj
        if isinstance(value, list):
            return tuple(self._native_argument(item) for item in value)
        return value

    def _reference(self, obj, path, expected_types=None):
        actual = self._actual_type(obj)
        if expected_types and not any(self.catalog.is_a(actual, name) for name in expected_types):
            raise Unsupported("Native reference type disagrees with the official catalogue")
        # Scoped IDs round-trip safely even after a child list is reordered.
        # Canonical path references remain accepted as explicit caller input.
        return {"$live_id": self._identity(obj), "object_type": actual}

    def _value(self, value, member=None, path=None, depth=0):
        if depth > 12:
            raise Unsupported("Native result exceeds the supported nesting depth")
        if value is None or isinstance(value, (str, bool, int, float)):
            return self._bounded_json(value)
        if isinstance(value, dict):
            return self._bounded_json(value)
        if isinstance(value, (list, tuple)):
            if len(value) > 4096:
                raise Unsupported("Native result requires pagination")
            return [self._value(item, depth=depth + 1) for item in value]
        if path is not None:
            return self._reference(value, path, (member or {}).get("ref_types"))
        if all(hasattr(value, name) for name in ("note_id", "pitch", "start_time", "duration")):
            fields = (
                "note_id",
                "pitch",
                "start_time",
                "duration",
                "velocity",
                "mute",
                "probability",
                "velocity_deviation",
                "release_velocity",
            )
            return {
                name: self._bounded_json(getattr(value, name))
                for name in fields
                if hasattr(value, name)
            }
        actual = self._actual_type(value)
        return {"$live_id": self._identity(value), "object_type": actual}

    def _read(self, obj, target, member, offset=0, limit=100):
        try:
            value = getattr(obj, member["name"])
        except AttributeError as error:
            raise Unsupported(
                "use_max_backend: property is missing from the Python binding"
            ) from error
        path = target["path"] + " " + member["name"]
        collection = member.get("collection") or member.get("value_type") == "list"
        if collection:
            try:
                total = len(value)
                selected = [value[index] for index in range(offset, min(total, offset + limit))]
            except (TypeError, AttributeError) as error:
                raise Unsupported("use_max_backend: native collection is not indexable") from error
            refs = member.get("ref_types") or (member.get("items") or {}).get("ref_types", [])
            data = [
                self._reference(item, path + " " + str(offset + index), refs)
                if refs
                else self._value(item)
                for index, item in enumerate(selected)
            ]
            return {
                "value": data,
                "offset": offset,
                "limit": limit,
                "total": total,
                "next_offset": offset + len(data) if offset + len(data) < total else None,
            }
        return {"value": self._value(value, member, path if member.get("ref_types") else None)}

    @staticmethod
    def _expected(payload, target):
        if payload.get("expected_identity") != target["identity"]:
            raise ValueError("stale_target: resolve this native object again before writing")

    def _set(self, obj, target, member, payload):
        self._expected(payload, target)
        if "value" not in payload:
            raise ValueError("A property value is required")
        if member.get("dynamic_bounds"):
            member = dict(member)
            try:
                for bound, attribute in member["dynamic_bounds"].items():
                    if bound not in {"minimum", "maximum"} or attribute not in {"min", "max"}:
                        raise ValueError("Unknown documented dynamic bound")
                    member[bound] = self._finite(getattr(obj, attribute), "native " + attribute)
                if member.get("minimum", -math.inf) > member.get("maximum", math.inf):
                    raise ValueError("Native minimum exceeds maximum")
            except Exception as error:
                raise Unsupported("native_bounds_unavailable: " + str(error)) from error
        value = self.catalog.validate_value(member, self._bounded_json(payload.get("value")))
        native = self._native_argument(value)
        before_read = self._read(obj, target, member, limit=4096)
        if before_read.get("next_offset") is not None:
            raise Unsupported("use_max_backend: full property readback exceeds Python write bounds")
        before = before_read["value"]
        if "expected_value" in payload and payload["expected_value"] != before:
            raise ValueError("stale_value: property changed before the write")
        proposed = value
        result = {
            "status": "preview",
            "target": target,
            "before": before,
            "value": proposed,
            "dry_run": payload.get("dry_run", True),
            "write_may_have_occurred": False,
            "signature_validation": member.get("signature_validation"),
        }
        same_reference = False
        reference_identity = None
        if isinstance(value, dict) and ("$ref" in value or "$live_id" in value):
            reference_identity = self._identity(native)
            current = getattr(obj, member["name"])
            same_reference = current is native or current == native
        if proposed == before or same_reference:
            return dict(result, status="no_change", readback=before)
        if result["dry_run"]:
            return result
        result["write_may_have_occurred"] = True
        try:
            setattr(obj, member["name"], native)
        except Exception as error:
            result.update({"status": "partial", "error": str(error)})
        else:
            result["status"] = "ok"
        try:
            actual = self._read(obj, target, member, limit=4096)["value"]
            exact = actual == proposed
            no_observable_change = proposed != before and actual == before
            reference_match = (
                reference_identity is not None
                and isinstance(actual, dict)
                and actual.get("$live_id") == reference_identity
            )
            verified = exact or reference_match
            kind = (
                "native_reference_identity" if reference_match else "exact" if exact else "mismatch"
            )
            if no_observable_change:
                verified, kind = False, "no_observable_change"
            elif (
                not exact
                and member.get("value_type") == "float"
                and isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and isinstance(proposed, (int, float))
                and not isinstance(proposed, bool)
                and math.isclose(actual, proposed, rel_tol=1e-7, abs_tol=1e-9)
            ):
                # Live may store parameters as float32. This tolerates only
                # post-write representation error, never pre-write no-op detection.
                verified, kind = True, "native_rounding"
            result.update(
                {
                    "readback": actual,
                    "verified": verified,
                    "exact_match": exact,
                    "verification_kind": kind,
                    "no_observable_change": no_observable_change,
                }
            )
            if not verified:
                result["status"] = "partial"
        except Exception as error:
            result.update(
                {"status": "partial", "readback": "unknown", "readback_error": str(error)}
            )
        return result

    def _call(self, obj, target, member, payload, offset, limit):
        arguments = self._bounded_json(payload.get("arguments", []))
        if member.get("native_argument_adapter") or member.get("dictionary_mode"):
            raise Unsupported(
                "use_max_backend: this documented dictionary API needs a typed native adapter"
            )
        arguments = self.catalog.validate_arguments(member, arguments)
        native_args = [self._native_argument(value) for value in arguments]
        function = getattr(obj, member["name"], None)
        if not callable(function):
            raise Unsupported("use_max_backend: function is missing from the Python binding")
        mutating = member.get("mutating", True)
        if mutating:
            self._expected(payload, target)
            if payload.get("dry_run", True):
                return {
                    "status": "preview",
                    "target": target,
                    "arguments": arguments,
                    "write_may_have_occurred": False,
                    "signature_validation": member.get("signature_validation"),
                }
        try:
            value = function(*native_args)
        except Exception as error:
            return {
                "status": "partial" if mutating else "error",
                "target": target,
                "error": str(error),
                "write_may_have_occurred": bool(mutating),
                "readback": "unknown" if mutating else None,
            }
        result = {
            "status": "ok",
            "target": target,
            "write_may_have_occurred": bool(mutating),
            "effect_verification": "not_available_for_function" if mutating else "read_only",
        }
        try:
            if (
                value is not None
                and not isinstance(value, (str, bytes, dict))
                and hasattr(value, "__len__")
            ):
                total = len(value)
                result.update(
                    {
                        "result": [
                            self._value(value[index])
                            for index in range(offset, min(total, offset + limit))
                        ],
                        "offset": offset,
                        "total": total,
                        "next_offset": offset + limit if offset + limit < total else None,
                    }
                )
            else:
                result["result"] = self._value(value)
        except Exception as error:
            # Serialization failure after a mutation is not permission to try another backend.
            result.update(
                {
                    "status": "partial" if mutating else "unsupported",
                    "error": str(error),
                    "readback": "unknown" if mutating else None,
                }
            )
        return result

    def _queue(self, subscription, event):
        if len(subscription["events"]) == subscription["queue_limit"]:
            subscription["events"].popleft()
            subscription["dropped"] += 1
        subscription["sequence"] += 1
        subscription["events"].append(
            dict(event, sequence=subscription["sequence"], monotonic_time=self.clock())
        )

    def _remove(self, subscription):
        if not subscription["active"] and not subscription.get("detach_pending"):
            return None
        subscription["active"] = False
        try:
            subscription["remove"](subscription["callback"])
        except Exception as error:
            subscription["detach_pending"] = True
            subscription["cleanup_error"] = str(error)
            subscription["next_detach_retry"] = self.clock() + 1.0
            return str(error)
        subscription["detach_pending"] = False
        subscription["cleanup_error"] = None
        return None

    def _drop(self, subscription_id, reason):
        # A failed detach stays in this bounded table, retaining its native
        # callback handle and consuming both global and owner quotas.
        subscription = self.subscriptions[subscription_id]
        error = self._remove(subscription)
        if error:
            subscription.setdefault("closed_reason", reason)
            subscription["last_cleanup_reason"] = reason
            try:
                self._schedule_expiry()
            except Exception as scheduling_error:
                self.scheduled = False
                subscription["scheduling_error"] = str(scheduling_error)
            return error
        self.subscriptions.pop(subscription_id)
        self.tombstones[subscription_id] = {
            "owner": subscription["owner"],
            "reason": reason,
            "cleanup_error": error,
            "expires": self.clock() + 300,
        }
        if len(self.tombstones) > 256:
            self.tombstones.pop(next(iter(self.tombstones)))
        return error

    def expire(self, allow_retry=True):
        now = self.clock()
        for subscription_id, subscription in list(self.subscriptions.items()):
            if subscription.get("detach_pending"):
                if (
                    allow_retry
                    and now >= subscription["next_detach_retry"]
                    and subscription.get("automatic_detach_retries", 0)
                    < self.MAX_AUTOMATIC_DETACH_RETRIES
                ):
                    subscription["automatic_detach_retries"] = (
                        subscription.get("automatic_detach_retries", 0) + 1
                    )
                    self._drop(subscription_id, subscription.get("closed_reason", "expired"))
            elif now >= subscription["expires"]:
                self._drop(subscription_id, "expired")
        for subscription_id, tombstone in list(self.tombstones.items()):
            if now >= tombstone["expires"]:
                del self.tombstones[subscription_id]

    def _schedule_expiry(self):
        scheduler = getattr(self.control_surface, "schedule_message", None)
        needs_tick = any(
            not record.get("detach_pending")
            or record.get("automatic_detach_retries", 0) < self.MAX_AUTOMATIC_DETACH_RETRIES
            for record in self.subscriptions.values()
        )
        if self.scheduled or not needs_tick or not callable(scheduler) or self.closed:
            return
        self.scheduled = True

        def tick():
            self.scheduled = False
            if self.closed:
                return
            self.expire()
            self._schedule_expiry()

        scheduler(1, tick)

    def _observe(self, obj, target, member, payload):
        self._expected(payload, target)
        owner = self._owner(payload)
        ttl = self._finite(payload.get("ttl_seconds", 60), "ttl_seconds", 1, 300)
        queue_limit = self._integer(payload.get("queue_limit", 64), "queue_limit", 1, 256)
        if len(self.subscriptions) >= self.MAX_SUBSCRIPTIONS:
            raise ValueError("Global observation limit reached")
        if (
            sum(record["owner"] == owner for record in self.subscriptions.values())
            >= self.MAX_OWNER_SUBSCRIPTIONS
        ):
            raise ValueError("Owner observation limit reached")
        add = getattr(obj, "add_" + member["name"] + "_listener", None)
        remove = getattr(obj, "remove_" + member["name"] + "_listener", None)
        if not callable(add) or not callable(remove):
            raise Unsupported("use_max_backend: native listener API is not exposed for this member")
        initial_read = self._read(obj, target, member)
        initial = initial_read["value"]
        subscription_id = "python:" + uuid.uuid4().hex
        record = {
            "owner": owner,
            "target": target,
            "expires": self.clock() + ttl,
            "events": deque(),
            "queue_limit": queue_limit,
            "dropped": 0,
            "sequence": 0,
            "remove": remove,
            "active": True,
        }

        def callback(*_args):
            if self.closed or subscription_id not in self.subscriptions or not record["active"]:
                return
            if self.clock() >= record["expires"]:
                self._drop(subscription_id, "expired")
                return
            try:
                current = self._read(obj, target, member)
                self._queue(record, {"status": "ok", **current})
            except Exception as error:
                self._queue(
                    record,
                    {"status": "error", "reason": "object_or_member_invalid", "error": str(error)},
                )
                record["closed_reason"] = "object_or_member_invalid"
                record["cleanup_error"] = self._remove(record)

        record["callback"] = callback
        self.subscriptions[subscription_id] = record
        try:
            add(callback)
        except Exception as error:
            cleanup_error = self._drop(subscription_id, "registration_failed")
            return {
                "status": "partial",
                "target": target,
                "subscription_id": subscription_id,
                "error": str(error),
                "listener_registration_may_have_occurred": True,
                "cleanup_error": cleanup_error,
            }
        scheduling_error = None
        try:
            self._schedule_expiry()
        except Exception as error:
            self.scheduled = False
            scheduling_error = str(error)
        return {
            "status": "partial" if scheduling_error else "ok",
            "target": target,
            "subscription_id": subscription_id,
            "value": initial,
            "initial_read": initial_read,
            "ttl_seconds": ttl,
            "queue_limit": queue_limit,
            "ttl_cleanup": "main_thread_scheduled" if self.scheduled else "on_request_or_callback",
            "scheduling_error": scheduling_error,
        }

    def _subscription(self, payload, op, offset, limit):
        owner = self._owner(payload)
        if op == "cleanup":
            ids = [key for key, record in self.subscriptions.items() if record["owner"] == owner]
            errors = [error for key in ids if (error := self._drop(key, "owner_cleanup"))]
            return {
                "status": "partial" if errors else "ok",
                "closed_count": len(ids) - len(errors),
                "detach_pending_count": len(errors),
                "pending_subscriptions": [key for key in ids if key in self.subscriptions],
                "cleanup_errors": errors,
            }
        subscription_id = payload.get("subscription_id")
        record = self.subscriptions.get(subscription_id)
        if record is None:
            old = self.tombstones.get(subscription_id)
            if old and old["owner"] == owner:
                return {
                    "status": "error",
                    "reason": old["reason"],
                    "cleanup_error": old["cleanup_error"],
                }
            raise ValueError("Unknown subscription or owner mismatch")
        if record["owner"] != owner:
            raise ValueError("Unknown subscription or owner mismatch")
        if op == "unobserve":
            error = self._drop(subscription_id, "unobserved")
            return {
                "status": "partial" if error else "ok",
                "subscription_id": subscription_id,
                "closed": error is None,
                "detach_pending": error is not None,
                "cleanup_error": error,
            }
        if offset:
            raise ValueError("poll drains a queue; offset must be zero")
        events = [record["events"].popleft() for _ in range(min(limit, len(record["events"])))]
        return {
            "status": "partial" if record.get("closed_reason") else "ok",
            "target": record["target"],
            "subscription_id": subscription_id,
            "events": events,
            "dropped_events": record["dropped"],
            "active": record["active"],
            "detach_pending": bool(record.get("detach_pending")),
            "remaining": len(record["events"]),
            "closed_reason": record.get("closed_reason"),
            "cleanup_error": record.get("cleanup_error"),
        }

    def _capabilities(self, obj, target, object_type, name, offset, limit):
        def describe(member):
            try:
                native = getattr(obj, member["name"])
            except AttributeError:
                present, availability, error = False, "unsupported", None
            except Exception as exception:
                present, availability, error = None, "unknown", str(exception)
            else:
                present, availability, error = True, "exposed", None
                if member["kind"] == "functions" and not callable(native):
                    availability = "unsupported"
            listener = "add_" + member["name"] + "_listener"
            remover = "remove_" + member["name"] + "_listener"
            return {
                "member": member,
                "native_attribute_present": present,
                "availability": availability,
                "getter_error": error,
                "native_listener_present": callable(getattr(obj, listener, None))
                and callable(getattr(obj, remover, None)),
                "write_or_call_behavior": "not_probed",
                "runtime_acceptance": "not_inferred",
            }

        if name is not None:
            return {
                "status": "ok",
                "target": target,
                **describe(self.catalog.resolve_member(object_type, name)),
            }
        names, current, seen = set(), object_type, set()
        while current is not None and current not in seen:
            seen.add(current)
            definition = self.catalog.object_definition(current)
            names.update(definition["members"])
            current = definition.get("parent")
        names = sorted(names)
        members = [
            describe(self.catalog.resolve_member(object_type, member_name))
            for member_name in names[offset : offset + limit]
        ]
        return {
            "status": "ok",
            "target": target,
            "members": members,
            "offset": offset,
            "limit": limit,
            "total": len(names),
            "next_offset": offset + len(members) if offset + len(members) < len(names) else None,
            "runtime_acceptance": "not_inferred",
        }

    def request(self, payload):
        target = None
        try:
            if self.closed:
                raise ValueError("Runtime is closed")
            if not isinstance(payload, dict):
                raise ValueError("A request object is required")
            self._bounded_json(payload)
            if not isinstance(payload.get("dry_run", True), bool):
                raise ValueError("dry_run must be a boolean")
            op = payload.get("op")
            self.expire(allow_retry=op not in {"cleanup", "unobserve"})
            offset = self._integer(payload.get("offset", 0), "offset")
            limit = self._integer(payload.get("limit", 100), "limit", 1, 1024)
            if op in ("poll", "unobserve", "cleanup"):
                return self._subscription(payload, op, offset, limit)
            if op not in ("resolve", "get", "set", "call", "observe", "capabilities"):
                raise ValueError("Unknown LOM operation")
            if op == "capabilities" and not payload.get("path"):
                return {
                    "status": "ok",
                    "backend": "python",
                    "runtime_scope": self.scope,
                    "operations": ["resolve", "get", "set", "call", "observe", "poll", "unobserve"],
                    "catalogue": self.catalog.describe(offset=offset, limit=limit),
                    "native_dictionary_calls": "use_max_backend",
                    "runtime_acceptance": "not_inferred",
                }
            obj, target = self._resolve(payload.get("path"), payload.get("object_type"))
            if op == "resolve":
                return {"status": "ok", "target": target, "write_may_have_occurred": False}
            if op == "capabilities":
                return self._capabilities(
                    obj, target, payload["object_type"], payload.get("member"), offset, limit
                )
            member = self.catalog.validate_operation(
                payload["object_type"], payload.get("member"), op
            )
            if op == "get":
                return {
                    "status": "ok",
                    "target": target,
                    **self._read(obj, target, member, offset, limit),
                    "write_may_have_occurred": False,
                }
            if op == "set":
                return self._set(obj, target, member, payload)
            if op == "call":
                return self._call(obj, target, member, payload, offset, limit)
            if op == "observe":
                return self._observe(obj, target, member, payload)
        except Unsupported as error:
            return {
                "status": "unsupported",
                "target": target,
                "reason": str(error),
                "write_may_have_occurred": False,
            }
        except Exception as error:
            return {
                "status": "error",
                "target": target,
                "error": str(error),
                "write_may_have_occurred": False,
            }

    def close(self):
        self.closed = True
        errors = [
            error
            for key in list(self.subscriptions)
            if (error := self._drop(key, "runtime_closed"))
        ]
        if not self.subscriptions:
            self.identities.clear()
            self.tombstones.clear()
        return {
            "status": "partial" if errors else "ok",
            "cleanup_errors": errors,
            "pending_subscriptions": [
                {
                    "subscription_id": key,
                    "target": record["target"],
                    "cleanup_error": record.get("cleanup_error"),
                }
                for key, record in self.subscriptions.items()
            ],
        }
