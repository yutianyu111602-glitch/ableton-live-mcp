# Official LiveAPI companion backend

This original MIDI-pass-through Max for Live device supplies the documented Max
LiveAPI transport for capabilities absent from the Python Remote Script transport.
It is a development candidate. The companion has loaded and exercised official
LiveAPI reads, writes, calls and observations in an independent Live 12.4.1 test
Set. This is representative native evidence, not acceptance of every operation
or audio/listening behavior. See [validation status](../NATIVE_VALIDATION.md).

Build with `python scripts/build_max_bridge.py` after the shared schema has been
generated. Keep `LOM_API_Bridge.amxd`, `max_lom_runtime.js`, `node_bridge.js` and
`lom_schema.json` together. Load exactly one device instance in a dedicated test
Set. It requires Max for Live; the Max host provides Node's `max-api` module.
No npm install, cloud service, credentials or arbitrary-code endpoint is used.

The M4L patch starts `node.script` with deferred messages. `live.thisdevice`
readiness and `deferlow` precede every creation/use of a LiveAPI object. Network
requests travel through Node's loopback listener into Max's low-priority queue.
Callbacks only enqueue observations. Subscriptions have bounded queues and TTLs;
stopping the device or disconnecting its last client releases observers.

## Framing

The default listener is `127.0.0.1:9878`. `ABLETON_MAX_PORT` may choose another
local port; binding another host is not supported. Frames are newline-delimited
UTF-8 JSON, with a 1 MiB request limit, eight TCP connections and 64 in-flight
requests. There is no automatic retry after a timeout or possible write.

```json
{"id":"request-1","request":{"op":"resolve","path":"live_set","object_type":"Song","owner":"client-uuid"}}
```

The response is `{"id":"request-1","result":{...}}`. Supported operations are
resolve/get/set/call/observe/poll/unobserve/capabilities and owner cleanup.
Identity strings are scoped as `max:<device-start-scope>:<LiveAPI.id>`.
Set and mutating calls require the expected identity and default to preview.
Returned child references carry their current scope; stale references are refused.

Native object IDs are signed, nonzero integers: registered ControlSurface objects
can have negative IDs. An ID of zero is an absent reference, never a valid target.
Object-reference setters use LiveAPI's three-argument `set(member, "id", id)`
calling convention. Reference nullability remains host-validated: Live 12.4.1
rejected clearing `Clip.groove` with ID zero. A rejected or unchanged setter
returns actual readback and a partial result; it is never silently retried.

Schema membership/access, actual object type, finite JSON and documented argument
forms are checked before native operations. If a member is absent in the running
Live build, it is reported unsupported. An API declaration is not a promise that
an older build can execute that member. Native failures after an attempted write
are reported as possibly written and are never routed to another backend.

The generated AMXD uses the uncompressed `ampf`/`meta`/`ptch` container layout
observed in Ableton's installed blank MIDI Effect template. Only the format is
used; no original patch, product identifier, artwork or code is copied. Structural
container validation and fake-LiveAPI tests are separate from actual Live loading.

Official references:

- https://docs.cycling74.com/apiref/js/liveapi/
- https://docs.cycling74.com/reference/node.script
- https://docs.cycling74.com/apiref/lom/
