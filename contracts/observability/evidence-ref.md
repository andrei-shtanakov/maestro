# EvidenceRef v1 — rationale

Contracts-roadmap phase 3 (RD-003). Schema: `evidence-ref.schema.json`.

## What this is

A **typed pointer to evidence**, not the evidence itself. Phase 0.5 recon
established that the ecosystem already mints all the keys needed to find
evidence about a work item; this contract only names the pointer shape so
records can carry references uniformly. Nothing is copied or embedded —
dereferencing stays with the owner of the underlying store.

## Kinds and required keys (normative)

| kind | required | dereferences to | owner of the store |
|------|----------|-----------------|--------------------|
| `trace` | `trace_id` (+opt `span_id`) | W3C trace across JSONL logs | observability contract (this dir) |
| `log` | `pipeline_id` | Maestro session `logs/<ULID>/` | Maestro |
| `benchmark` | `run_id` | `benchmark_runs` row | arbiter (`report_benchmark`) |
| `decision` | `decision_id` | decisions row / PolicyDecisionRef | arbiter (`contracts/policy-decision-ref/`) |
| `artifact` | `project` + `path` | file in the owning repo (project-relative, never absolute) | that project |

The conditional requirements are enforced in the schema (`allOf`/`if`).
`note` is a human hint and must never be machine-parsed.

## Relation to WorkCorrelation

`WorkCorrelation` carries `evidence_refs[]` as an optional field (added in
this phase; it was deliberately absent from the phase-1 cut so it would not
point into a vacuum before this contract existed). The inline
`evidence_ref` definition in `contracts/work-correlation/schema.json` is a
byte-equal copy of this schema's object shape — a sync test in
`tests/test_correlation_contract.py` keeps the two from drifting.

Adding an optional field pre-adoption is treated as additive: no consumer
had vendored the phase-1 file at the time of the change (dispatcher
computes correlation read-side and does not validate records against the
schema). Post-adoption, the same change would have required a version bump.

## What v1 deliberately is not

- Not a transport or emitter mandate: how refs get attached to records is
  the producer's business; read-side computation remains valid.
- Not a URI scheme: kinds+keys are structured fields, not encoded strings —
  cheaper to validate, no parsing ambiguity.
- Not an integrity claim: a ref may dangle (store purged, 90-day retention
  in arbiter, logs rotated). Consumers must treat dereference failures as
  "evidence expired", not as contract violations.

## Reference implementation

`maestro/correlation.py`: `EvidenceRef` model (strict, kind-conditional
validation) + builders (`trace_evidence`, `log_evidence`,
`benchmark_evidence`, `decision_evidence`, `artifact_evidence`), and
`WorkCorrelation.evidence_refs`. Golden fixtures under
`contracts/work-correlation/fixtures/` and this directory's tests validate
live builder output against the schema.
