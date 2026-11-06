"""Full official LOM access through a bounded catalogue, not arbitrary execution."""

import json
from enum import Enum
from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field, JsonValue

from ..app import mcp
from ..lom_backend import dispatch
from ..lom_catalog import LomCatalog

CATALOG = LomCatalog()
ObjectType = Enum(
    "LomObjectType", {name.replace(".", "_"): name for name in CATALOG.schema["objects"]}, type=str
)
Backend = Literal["auto", "python", "max"]
Path = Annotated[
    str,
    Field(
        min_length=1,
        max_length=2048,
        description="Official LOM path, e.g. live_set tracks 19 arrangement_clips 0. Only documented children and numeric indices are accepted.",
    ),
]
Identity = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Exact backend-bound identity returned by lom_resolve, e.g. python:<scope>:<id> or max:<scope>:<id>.",
    ),
]
Member = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description="Exact member name returned by lom_describe for the object class.",
    ),
]
Limit = Annotated[int, Field(ge=1, le=1024)]
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)


def _request(op, path, object_type, backend, mutating=None, **fields):
    path = CATALOG.validate_path(path)
    if fields.get("expected_value") is None:
        fields.pop("expected_value", None)
    return json.dumps(
        dispatch(
            dict(op=op, path=path, object_type=object_type.value, **fields),
            backend,
            mutating=mutating,
        ),
        indent=2,
    )


@mcp.tool(annotations=READ)
def lom_describe(
    ctx: Context,
    object_type: ObjectType | None = None,
    member: str | None = None,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Limit = 100,
) -> str:
    """Browse all 45 official LOM objects and 588 declared members offline, including access and signatures.

    The catalogue describes documentation coverage, not current-host support.
    Resolve an actual target and inspect capabilities before writing. Inherited
    members retain their declaring class. No running Live is needed.
    """
    return json.dumps(
        CATALOG.describe(object_type.value if object_type else None, member, offset, limit),
        indent=2,
    )


@mcp.tool(annotations=READ)
def lom_resolve(
    ctx: Context, path: Path, object_type: ObjectType, backend: Backend = "auto"
) -> str:
    """Resolve a documented object path to actual runtime type and backend-bound identity.

    Indices can move. Every subsequent actual mutation must supply this identity;
    resolving a missing/version-gated object does not fabricate a usable target.
    The Max backend requires the companion device loaded in Live Suite.
    """
    return _request("resolve", path, object_type, backend)


@mcp.tool(annotations=READ)
def lom_get(
    ctx: Context,
    path: Path,
    object_type: ObjectType,
    member: Member,
    backend: Backend = "auto",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Limit = 100,
) -> str:
    """Read any documented get-capable property/child, paging lists and returning typed object references.

    Access is checked against the official class and inheritance. No arbitrary
    attributes, Python expressions or Max messages are accepted.
    """
    CATALOG.validate_operation(object_type.value, member, "get")
    return _request("get", path, object_type, backend, member=member, offset=offset, limit=limit)


@mcp.tool(annotations=WRITE)
def lom_set(
    ctx: Context,
    path: Path,
    object_type: ObjectType,
    member: Member,
    value: JsonValue,
    expected_identity: Identity,
    backend: Backend = "auto",
    expected_value: JsonValue = None,
    dry_run: bool = True,
) -> str:
    """Preview/set a documented writable LOM property with type, target and optional old-value guards.

    Defaults to preview. This can alter audio, playback, devices or content. Use
    an authorized target and inspect before/after. Unknown write outcomes never
    trigger fallback or retry on another backend. References use {$ref: path,
    object_type: class}; no arbitrary file or code execution is provided.
    """
    definition = CATALOG.validate_operation(object_type.value, member, "set")
    value = CATALOG.validate_value(definition, value)
    return _request(
        "set",
        path,
        object_type,
        backend,
        member=member,
        value=value,
        expected_identity=expected_identity,
        expected_value=expected_value,
        dry_run=dry_run,
    )


@mcp.tool(annotations=WRITE)
def lom_call(
    ctx: Context,
    path: Path,
    object_type: ObjectType,
    member: Member,
    arguments: Annotated[list[JsonValue], Field(max_length=256)] = [],
    expected_identity: Identity | None = None,
    backend: Backend = "auto",
    dry_run: bool = True,
) -> str:
    """Call any documented LOM function with bounded JSON arguments and signature validation.

    Mutating calls default to preview and require a resolved target identity.
    Functions can delete, record, duplicate, change routing or transmit MIDI;
    authorization must cover the actual requested action. Getters return real
    native results. Dictionary and object-reference arguments follow lom_describe.
    API/version errors remain explicit; an uncertain mutation is not retried.
    """
    definition = CATALOG.validate_operation(object_type.value, member, "call")
    arguments = CATALOG.validate_arguments(definition, arguments)
    return _request(
        "call",
        path,
        object_type,
        backend,
        mutating=definition.get("mutating", True),
        member=member,
        arguments=arguments,
        expected_identity=expected_identity,
        dry_run=dry_run,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def lom_observe(
    ctx: Context,
    path: Path,
    object_type: ObjectType,
    member: Member,
    expected_identity: Identity,
    backend: Backend = "auto",
    ttl_seconds: Annotated[int, Field(ge=1, le=300)] = 60,
    queue_limit: Annotated[int, Field(ge=1, le=256)] = 128,
) -> str:
    """Observe a documented observable property/child using a real native callback and bounded queue.

    Subscription IDs belong to this MCP process. Poll explicitly; stop when
    finished. TTL, queue and disconnect cleanup prevent unbounded listeners.
    Notifications only enqueue reads; they do not write Live from callbacks.
    """
    CATALOG.validate_operation(object_type.value, member, "observe")
    return _request(
        "observe",
        path,
        object_type,
        backend,
        member=member,
        expected_identity=expected_identity,
        ttl_seconds=ttl_seconds,
        queue_limit=queue_limit,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def lom_poll(
    ctx: Context,
    subscription_id: Annotated[str, Field(min_length=1, max_length=256)],
    limit: Limit = 100,
    backend: Backend = "auto",
) -> str:
    """Drain up to limit queued events for this process's subscription, with timestamps/drop counts."""
    return json.dumps(
        dispatch(dict(op="poll", subscription_id=subscription_id, limit=limit), backend), indent=2
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def lom_unobserve(
    ctx: Context,
    subscription_id: Annotated[str, Field(min_length=1, max_length=256)],
    backend: Backend = "auto",
) -> str:
    """Stop this process's subscription and release its native listener/queue."""
    return json.dumps(
        dispatch(dict(op="unobserve", subscription_id=subscription_id), backend), indent=2
    )


@mcp.tool(annotations=READ)
def lom_capabilities(
    ctx: Context,
    path: Path,
    object_type: ObjectType,
    backend: Backend = "auto",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Limit = 100,
) -> str:
    """Compare documented inherited members with actual runtime get/set/call/observe support.

    Unsupported and unknown remain separate. This performs no speculative write
    or function call, and presence checks do not constitute behavior acceptance.
    """
    return _request("capabilities", path, object_type, backend, offset=offset, limit=limit)
