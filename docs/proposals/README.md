# Proposals — design notes, not documentation

**Nothing in this directory describes current behaviour.** These are design
notes: some shipped and drifted since, some were superseded, some were never
built. For what cabal does today, read [`../architecture.md`](../architecture.md)
and the README; for why it does it, read
[`../../CABAL-DECISIONS.md`](../../CABAL-DECISIONS.md).

They are kept rather than deleted because the reasoning is worth more than the
conclusions — several decisions in CABAL-DECISIONS.md only make sense next to
the proposal they came from.

| file | status |
|---|---|
| `guardrails.md` | **shipped and drifted** — the guardrail foundation ships (`src/cabal/core/guardrails.py`); `VerdictType` also has `TERMINATE`, which the proposal omits |
| `activity-log-borrow-from-pi.md` | **shipped and drifted** — `TOOL_USE` now fires and `ActivityLogger` writes durable rows (`src/cabal/core/observability/activity_logger.py`, PRs #53-#55); the gap it describes is closed |
| `federated-singletons.md` | **partly shipped** (`cbl persona graduate`/`absorb`, federated agent kind, remote dispatch); the rest is not built |
| `federated-bulletin-and-router.md` | **partly shipped** (federated bulletin kind, remote dispatch in the router); the rest is not built; pairs with the above |
| `investigation-engine.md` | **partly shipped** (`AbortGraph`, streaming, detached tasks); the rest is not built |
| `cabal-tui.md` | **partly shipped** — the TUI exists (`src/cabal/tui/`, `cabal` entry point); the document still says it does not |
| `viewer-topology-and-persona-executors.md` | **superseded by its own banner**, same day it was written |
| `sherlock.md` | redirect to `investigation-engine.md` |

## What to distrust in them

- **Build-status claims.** `cabal-tui.md` says the `cabal` entry point "does not
  exist yet"; it exists. Check the code, not the proposal.
- **Cross-references.** Several cite the original Cabal design document, which this repo does not have
  (#202), and one points into a sibling repo that is not public.
- **Example names.** Worked examples use placeholder operators. Where a real
  first name survives, it is an artifact of who was in the room, not a product
  concept.
