# benchmark-contract — report_benchmark

Single source of truth for the `report_benchmark` MCP tool wire format
(Maestro → Arbiter). Owned by Maestro repo; consumed by both Maestro
(`tests/test_benchmark_contract.py`) and arbiter-mcp
(`tests/contract_test.rs`).

## Files

- `report_benchmark-v1.schema.json` — JSONSchema draft 2020-12,
  request + response under `definitions/`. Match against
  `#/definitions/Request` for incoming arguments,
  `#/definitions/Response` for outgoing tool result.

## Versioning

Two independent version axes (see Maestro spec
`docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md` §6):

- **`payload_version`** (constant `"1.0.0"` in v1): bumps on
  removed/reshaped fields. Additive optional fields don't bump.
- **MCP `protocolVersion`** (set by arbiter at `initialize`):
  tracks tool surface; bumps on new/removed tools.

## Arbiter CI fetch

Arbiter `tests/contract_test.rs` reads this file at build time.
Recommended mechanism (chosen at arbiter-side implementation):

- Option A: `git submodule` of Maestro pinned at SHA matching
  `ARBITER_PINNED_SHA` round-trip in Maestro CI.
- Option B: HTTP fetch in `build.rs` from
  `https://raw.githubusercontent.com/andrei-shtanakov/maestro/<SHA>/contracts/benchmark/report_benchmark-v1.schema.json`.
- Option C: Copy-on-bump (manual sync, CI grep guards against drift).

Arbiter side decides; Maestro side guarantees the file does not
move once committed.
