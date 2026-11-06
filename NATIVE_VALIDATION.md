# Full LOM candidate validation

Candidate: **1.8.1+fullapi.2**. The pinned official documentation describes Live
12.4.5; real host testing used **Live 12.4.1 on Windows** in an independent test
Set through MCP. No GUI automation was used. Real API behavior, offline routing
coverage and audio/product acceptance are separate evidence stages.

## Implemented and reproducible offline

- All 45 catalogue entries, 588 declared members and 1107 documented operations
  have bounded get/set/call/observe routes. These are operations, not 1107 tools;
  nine generic tools bring the full server to 175 tools.
- `api/full_lom_coverage.json` records every declaration and operation. Inherited
  members resolve under their declaring class. Eleven real documentation gaps
  remain explicit and require native signature/type validation.
- Catalogue, Python native-object and strict fake-LiveAPI tests cover validation,
  identity/reference binding, preview, uncertain writes, float32 readback,
  observations, framing, packaging and installation. Exhaustive fake dispatch
  over the catalogue is not exhaustive native acceptance.

## Real native evidence

Across the selected traces, 41 catalogue classes resolved directly; `Device` was
evidenced through subclasses and `this_device` as a root alias. DeviceIO and
TuningSystem lack completed fixtures. Deduplicated under declaring classes, the
traces contain 33 getter member IDs, five setter IDs with verified readback,
16 invoked function IDs and one controlled callback member ID. Reported presence
covers 359 declaring members. These counts measure different stages and cannot be
added into a native functional pass rate.

Both backends exercised target resolution, paged reads, preview without writes,
tempo change/restoration, real controlled-value callbacks, stale-identity
rejection, unsubscribe and application-version calls.

Python MIDI timing edits used native note IDs without snapping to a grid. The
serialized non-timing fields and IDs were preserved; the inverse edit restored
the original fingerprint. This fixture did not contain MPE expression events.
Max exercised dictionary note creation, reading and timing edits, plus an inverse
invocation. Its trace has no independent post-inverse getter. The requested
0.782-beat time read as 0.7820000312187813: the original strict check failed,
while a separately derived one-float32-ULP check passes.

Positive object-reference setters changed selected track and restored it, with
independent readback at each step. Native reference calls duplicated owned clips
into Arrangement fixtures. Signed ControlSurface IDs, registered concrete
subclasses, `this_device` and its parent resolved; a read-only control-name query
worked. No hardware control was grabbed or MIDI sent.

Representative stock devices, nested racks/chains/drum objects, views, Groove,
CuePoint, TakeLane, an audio clip and a self-generated silent Sample were probed.
Resolution and reported member exposure do not establish DSP behavior. Existing
test-track identities and clip data were checked around fixture creation.

The retained local automation repair inferred positive holds to the next point
and actual clip end. Mid-interval values, envelope clearing and parameter
restoration were read back. The exact beat-zero query returned zero despite a
requested one, so complete boundary behavior is not accepted.

## Known limits and pending acceptance

- Live 12.4.1 rejected clearing `Clip.groove` through a zero-ID reference with
  `set: type mismatch`. Readback remained unchanged. Nullable reference metadata
  is host-validated; it is not a promise that every setter accepts null. The
  result remains partial, with actual readback and no automatic retry/fallback.
- The helper fixture exposed no actual DeviceIO bus objects. TuningSystem lacks
  a completed native fixture in this evidence set. Neither is counted as a pass.
- Some Python native structured return/call conventions differ from Max. An
  unsupported safe read may use Max; identities and actual mutations stay bound
  to their resolved backend. Uncertain writes are never retried on another one.
- Final fullapi.2 source fixes passed offline verification. The complete final
  paired Python/Max payload has not yet been activated and retested in Live;
  native traces span earlier candidate payloads and individually tested fixes.
- The final pre-write guard detected a different music Set playing. That fixture
  load was aborted before mutation; all further native writes, installation and
  reloads were stopped. Resume only in the independent test Set with fresh
  context and identity checks.
- Undo, MPE fixture preservation, all 1107 native behaviors, save/reopen
  persistence, device GUI, hardware, recorded audio and listening remain pending.
  General LOM access does not expose arbitrary custom Max-device internals.

Raw native traces, project fixtures, local paths and private task logs stay local.
Only sanitized results and source are published. This branch is a personal-fork
candidate, not an upstream release, PyPI package or MCP Registry publication.
