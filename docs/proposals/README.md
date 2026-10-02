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
| `guardrails.md` | live design reference |
| `activity-log-borrow-from-pi.md` | live design reference |
| `federated-singletons.md` | proposal, not built |
| `federated-bulletin-and-router.md` | proposal, not built; pairs with the above |
| `investigation-engine.md` | proposal, not built |
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
