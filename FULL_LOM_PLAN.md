# Complete official LOM integration

Target: expose every documented operation of all **45 classes and 588 declared
members** in the pinned official Live 12.4.5 LOM through a bounded MCP interface.
Existing 166 tools remain compatible. New access is grouped by operation rather
than registering hundreds of nearly identical tool names.

## Architecture

1. `lom_schema.json` contains every declared member, its type, access, function
   signature, minimum version and inheritance. `LomCatalog` performs shared
   validation. Source documentation discrepancies remain explicit.
2. Nine MCP tools provide catalogue browsing, target resolution, property/child
   reads, guarded writes, function calls, observation start/poll/stop, and actual
   runtime capability inspection. Class names are a schema enum; member access
   must exist in that class or its documented inheritance.
3. The existing Python Remote Script adds a catalogue-controlled native backend.
   It navigates only documented children, calls only documented functions, and
   uses native listeners. Complex Python/Max calling-convention differences are
   reported before invocation rather than guessed.
4. An optional MIDI-through Max for Live companion uses official `LiveAPI` with
   low-priority scheduling. A bounded loopback Node for Max socket connects it to
   MCP. It supports the complete official get/set/call/observe transport,
   including dictionaries and native object-ID references.
5. An object identity includes backend, runtime scope and actual object ID.
   Writes remain bound to the resolved backend. No uncertain write is retried or
   sent to a second backend. Previews do not call setters or mutating functions.
6. Observations have per-client ownership, queue/TTL limits, overflow counts and
   explicit cleanup on stop/disconnect. Notifications enqueue observations only;
   they never mutate the Set from a callback.

## Implementation sequence

| Stage | Concrete acceptance |
| --- | --- |
| A. Schema | Exact equality with all 588 pinned identifiers; all get/set/call/observe operations derived; inherited members resolve; every formal signature parsed or its actual documentation gap identified. |
| B. Python backend | Real native navigation/reads/writes/calls/listeners, identity guards, references, paging and cleanup; only genuinely unavailable or non-equivalent runtime calls report unsupported. |
| C. Official Max backend | Loadable companion, low-priority official LiveAPI access, dictionaries/references, observations and actual TCP framing; no generic eval/code/Max-message escape hatch. |
| D. MCP and installation | All 175 tools discoverable; schema/data/helper files packaged and installed together; old installation retained; matched payload and safe stdio call verified. |
| E. Offline proof | Positive and negative cases for all catalogued operations, plus behavioral native-object and LiveAPI test doubles, transport partial failures, bounded observers, installation and source/wheel contents. |
| F. Live acceptance | Independent test Set; actual host capabilities, representative instances of each class, reads/writes/functions/observations, object deletion/reorder, Undo/expression preservation and saved/reopened evidence. |

Code-level integration, offline routing coverage and real Live acceptance are
separate records. A class absent from the test Set is `needs_fixture`, not proof
that its API is unsupported. An older Live build can be version-gated while the
interface itself is implemented. Documentation gaps do not justify fabricated
types, keys, success values or acceptance evidence.

## Release and continuity

Development is isolated on `feat/full-lom-api` from the first published candidate.
The earlier 166-tool installation remains the recoverable baseline while this
implementation is built and checked. Every new release includes paired bridge
payloads, the Max companion, an exact coverage report and native acceptance status.

The existing personal-fork publication authorization applies to updated candidate
code in this project. Upstream PRs follow isolated Live acceptance; PyPI/MCP
Registry identities and publication remain separate. Private logs, project files,
credentials, local configuration and machine-specific state remain excluded.
