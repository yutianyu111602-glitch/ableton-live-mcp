# API coverage and test interfaces

This branch is an independent development candidate based on upstream 1.8.1
(`55abd3a38a74c39f6f2fc0d1d90107d06ec5f952`). It retains the MIT license and upstream
credits. It does not claim an Ableton endorsement or an upstream release.

## Source of truth

The [official Live Object Model](https://docs.cycling74.com/apiref/lom/)
currently describes Live **12.4.5**. It is the Max for Live API. The Python Remote
Script transport used here has overlapping capabilities, but names, calling
conventions and runtime support must be verified individually. A document entry
does not establish that the Python bridge or an older Live version supports it.

`api/lom_inventory.json` pins all **45 object pages and 588 declared members**
found in that official index. It retains names, access modes, version labels,
source URLs and source hashes rather than copying documentation prose. Inherited
members are represented by their declaring object; counts are not tool counts.

`source_candidates` identifies source-name matches for review. It is deliberately
not an implemented-coverage score. `coverage_overrides.json` holds explicit reviewed
mappings. `implemented_offline` and `runtime_acceptance: not_run` are distinct.
Python-only capabilities and unsupported custom actions are tracked separately in
`api/python_extensions.json`.

Refresh documentation explicitly with:

```console
python scripts/sync_lom_inventory.py --refresh
git diff -- api/
```

The default command validates the pinned snapshot offline. Refresh never edits
Live, commits code, or publishes a release. Review added/removed/version-changed
members, implement bounded typed interfaces, and update the mapping with evidence.

## First iteration: practical test blockers

| Interface | Purpose and boundary |
| --- | --- |
| `get_test_context` | Set/transport/selection and runtime identity; unavailable file path/dirty flags stay unknown. |
| `get_clip_snapshot` | Session or Arrangement clip markers and paged notes with native IDs, release velocity and fingerprint. |
| `modify_notes_by_id` | Explicit per-note timing/attribute edits, milliseconds or beats, native vector, preview by default, identity guard, readback and inverse. No remove/add or grid snapping. |
| `get_device_test_state` | Nested Rack paths, parameter enabled/automation/state fields, capabilities and target identity. |
| `set_test_device_parameter` | Nested Rack parameter preview/write with target and optional previous-value guards, disabled-state rejection, actual readback and restore data. |
| `get_parameter_samples` | Bounded observations with actual UTC/song time; never starts transport or writes controls. |
| `get_clip_automation_snapshot` | Sample an existing envelope at explicit times; does not create or clear one. |
| `get_max_device_info` | Actual Max parameter bank capabilities; does not guess custom button indices. |
| `set_plugin_editor_state` | Identity-guarded preview/open/close only if the runtime exposes `is_editor_open`. |
| `verify_saved_set` | Validate a named disk ALS and hash; never claims current unsaved RAM matches it. |

`write_automation` now accepts an explicit nonnegative `duration` for each step.
All points, native value ranges and finite endpoints are validated before clearing
the old envelope. Legacy omitted durations remain zero and return a warning.
Envelope replacement is destructive and is not a transactional rollback guarantee.

The existing local Windows save repair is retained: it requires a specific
existing ALS, preserves its previous bytes, and verifies a changed valid file plus
a clean window title. Save As and never-saved sets still require a native GUI
boundary. No generic Save As API is fabricated.

## Validation stages

1. **Source and schema:** typed inputs, annotations, tool dispatch, old interface
   compatibility, finite/range checks and complete preflight.
2. **Offline behavior:** fake native vectors, unchanged note IDs/opaque expression
   data, dry-run zero writes, stale targets, partial failures, inverse edits,
   automation duration and unsupported capabilities.
3. **MCP process:** initialize, tools/list and a safe local call over real stdio.
4. **Live read-only:** verify package/bridge versions, context, nested targets,
   snapshots and capability fallbacks on each supported Live version.
5. **Isolated Live writes:** a designated disposable test Set, preserved original,
   exact note IDs and expression data before/after, Undo, sustained envelope
   evaluation, editor state, saved artifact and reopen evidence.
6. **Product acceptance:** separate VST/M4L GUI, LFO release, audio function and
   listening cases. Source/tests/parameter readback do not satisfy this stage.

This candidate has not yet passed stages 4–6. Current user music projects must
not be used as a write-test fixture. A bridge reload invalidates fingerprints.

## Subsequent iterations

1. **Finish test acceptance:** validate the first iteration in the designated
   host, including nonzero clip source markers, hidden notes, expression events,
   inverse edits/Undo, automation step persistence and unsupported-version behavior.
2. **Rack calibration and automation:** bounded multi-target snapshots/writes with
   complete preflight, recorded restore values, nested macro mapping discovery and
   validated host application/save paths. Retain disabled/remote-controlled states.
3. **M4L adapter contract:** only with an explicit device-side protocol, expose
   named Read/Preview/Build/Clear operations with operation IDs and status. General
   LOM access does not grant access to arbitrary internal Max UI objects.
4. **Audio and plug-in evidence:** distinguish parameter state, level meters,
   gain reduction when actually exposed, loaded binary identity, recorded audio
   measurements, and listening. Do not substitute one for another.
5. **Expand by official object family:** Clip/Clip.View/ClipSlot; Track/Scene/Song;
   Device/DeviceParameter/Rack/Chain/Drum; routing and mixer; Sample/Simpler; native
   device subclasses; application/view/tuning. For every get/set/call/observe
   capability record transport, minimum version, side effects, pagination,
   validation, test evidence and unavailable states.
6. **Observation lifecycle:** add bounded subscriptions with explicit start/stop,
   queue limits and reconnect cleanup; never run an unbounded main-thread poller.

An object-family iteration is complete only when every catalogued member has an
explicit supported/tested, unsupported/version-gated, device-contract-required,
or reviewed-deferred disposition. Coverage percentages must name their evidence
stage and denominator.

## GitHub contribution and release workflow

Recommended: maintain a personal **fork**, keep upstream `main` unchanged, develop
topic branches, and submit small Pull Requests to upstream. GitHub does not grant
push access to the original author’s repository through a fork. The maintainer
decides whether to merge an upstream PR. This repository’s CONTRIBUTING.md
welcomes bug reports and PRs and requires tests/lint plus paired bridge updates.

Use separate PRs for the automation duration repair, native note editing, and
diagnostic interfaces where practical. A draft PR can carry explicit native-test
gaps. Keep the API inventory/roadmap review separate from runtime behavior claims.

Public upstream forks are public. Private project logs, local absolute paths,
machine/user identifiers, credentials, music, ALS files and product fixtures are
excluded from this branch’s publication set. `work/` and local CONTROL.md stay
untracked. Preserve the original license and authorship.

GitHub source publication, a GitHub prerelease, PyPI publication and MCP Registry
publication are different actions. A fork must not publish under upstream’s
registry identity or package release process. This branch guards upstream’s
existing PyPI/MCP Registry jobs so they run only in the upstream repository.
Choose independent registry identities explicitly before any future registry
publication. A local development version is not a publicly downloadable package.

Before publishing: review the exact tracked diff, run CI, name the destination
fork and branch, prepare the PR/release text, then authorize the external write.
Local candidates and tests do not automatically open PRs, notify maintainers or
release artifacts.
