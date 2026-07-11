# WorkCorrelation v1 — rationale

Contracts-roadmap phase 1 (RD-001). Canon narrative:
`prograph-vault/authored/notes/2026-07-11-ai-dark-factory-consolidated-roadmap.md`;
key audit: `.../2026-07-11-phase05-correlation-recon.md`.

## What this is

A **thin correlation record**, not a universal WorkItem. Each project keeps
its own local schema and lifecycle; this contract only fixes the join key and
a lossy common status so cross-project drill-down
(`spec → DAG task → routing decision → outcome → run`) works without a shared
runtime.

## Minter and key inheritance

- **Maestro is the minter**: `work_item_id` = Maestro `task.id` /
  `workstream.id`. Decided by evidence, not fiat — Maestro already passes
  `task.id` verbatim to arbiter `route_task` (`coordination/routing.py`), so
  the key flows into `decisions.task_id` / `outcomes.task_id` today.
- **No new id is minted.** Phase 0.5 recon confirmed existing keys suffice:
  `task.id` (work), `pipeline_id` ULID (run, = `logs/<ULID>/` dir name),
  W3C `trace_id` (observability contract).
- **Children derive deterministically**: a spec-runner task inside a
  workstream's spec gets `work_item_id = "<parent>/<TASK-nnn>"` with
  `parent_work_item_id = <parent>` and `source_locator = <spec_dir>`.
  This is the spec↔DAG bridge: spec-runner's `TASK-nnn` is only unique per
  spec dir, so the pair (locator, local id) plus the parent link fixes it.

## Status: surjective projection, never a bijection

Source vocabularies are incompatible by construction (live example: Maestro
once sent `interrupted` to arbiter's `success|failure|timeout|cancelled` enum
— Maestro #65). The common enum is a **lossy projection for drill-down**;
`source_status` is kept verbatim so no information is lost.

Common enum: `pending | running | needs_review | done | failed | cancelled`.
Universal exits: `failed`, `cancelled` (reachable from any non-terminal
state). Recovery: `pending` (retry re-enters the queue). Terminal: `done`,
`cancelled`.

### Projection tables (normative)

| vocabulary `maestro.task` | common |
|---|---|
| pending, ready | pending |
| awaiting_approval | needs_review |
| running, validating | running |
| done | done |
| failed | failed |
| needs_review | needs_review |
| abandoned | cancelled |

| vocabulary `maestro.workstream` | common |
|---|---|
| pending, ready | pending |
| decomposing, running, merging | running |
| pr_created | needs_review |
| done | done |
| failed | failed |
| needs_review | needs_review |
| abandoned | cancelled |

| vocabulary `spec-runner.task` | common |
|---|---|
| pending | pending |
| running | running |
| success | done |
| failed | failed |
| skipped | cancelled |

| vocabulary `arbiter.outcome` | common |
|---|---|
| success | done |
| failure, timeout | failed |
| cancelled | cancelled |

`awaiting_approval` and `pr_created` map to `needs_review` deliberately: the
common bucket means "waiting on a human", which is exactly what drill-down
needs to surface.

**Out of scope:** arbiter `decisions.action` (`assign|reject|fallback`) is a
policy-decision vocabulary, not a work-item lifecycle — it belongs to
`PolicyDecisionRef v1` (roadmap phase 2).

## Deliberately absent from v1

- `evidence_refs[]` — arrives in phase 3 on top of the graduated
  observability contract; adding it now would point into a vacuum.
- Any transport/emitter mandate. v1 is the record shape plus the projection;
  consumers may compute records read-side (as dispatcher `/api/work-items`
  already does) without any project emitting anything new.

## Consumers and versioning

Consumers (dispatcher, arbiter, steward, atp) vendor a pinned copy of
`schema.json` per ecosystem contract policy. Reference implementation:
`maestro/correlation.py` (projection tables are asserted total against the
source enums in `tests/test_correlation_contract.py`; golden fixtures under
`fixtures/` validate against the schema).

Breaking changes bump `schema_version` and add a new schema file; the
projection tables above are part of the contract surface.
