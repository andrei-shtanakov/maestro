# contracts/halt-admission/v1

The admission rule of the DarkFactory halt: **may new agent work start on
this repository?** Every actor that admits new work — devtools `merge-pr.sh`
and its governance runner, dispatcher's run controller, spec-runner and
maestro when asked to — applies this one rule. Consumers **vendor a pinned
copy** of this directory and test their implementation against
`vectors.json`; the reference implementation is `github_checker/halt_gate.py`
(`github-checker halt-gate <dir>`).

## Inputs

What a **write-level** token can read (the agent's own; D0 showed a non-admin
gets `bypass_actors: null`, so the gate never judges the bypass list):

1. `GET repos/{owner}/{repo}/rulesets?includes_parents=false` (paginated) —
   the listing, `[{id, name, ...}]`. Unreadable → `listing: null`.
2. Only when exactly one ruleset is named `darkfactory-halt`:
   `GET repos/{owner}/{repo}/rulesets/{id}` — its detail. Unreadable →
   `detail: null`.

## The rule

| what the reader sees | admit | code |
|---|---|---|
| listing unreadable | no | `refuse_unknown` |
| no ruleset named exactly `darkfactory-halt` | **yes** | `admit_missing` |
| two or more of that name | no | `refuse_duplicate` |
| detail unreadable | no | `refuse_unknown` |
| `enforcement == "disabled"` | **yes** | `admit_off` |
| `enforcement == "active"` | no | `refuse_on` |
| any other or absent enforcement | no | `refuse_enforcement` |

`admit_missing` is the owner's decision (2026-10-01): only an admin can
delete or disable the ruleset, and an admin bypasses the halt anyway, so a
missing ruleset is a forgotten arming — which dispatcher's admin read shows
as a deviation — never an agent's way around the halt. Refusing would block
every repository outside the armed fleet.

**Consumer-level rule — `admit_not_github`:** a checkout whose
`remote.origin.url` host is not `github.com` has no forge halt — admit. A
checkout with NO origin is not GitHub either — admit. A GitHub origin whose
owner/name cannot be resolved is unreadable — refuse. The discriminator is
the origin URL's host, exactly; `origin_vectors` in `vectors.json` pins it
(reference: `github_checker.halt_gate.is_github_origin`). Consumers that only
ever act on GitHub repositories (devtools, dispatcher) need not apply it.

## The answer is `admit`, never an exit code

`github-checker halt-gate` follows actions/v1: exit 0 means "answered"
(`ok: true`), not "admitted" — a refusal of a halted repository exits 0
with `admit: false`. An unreadable halt exits 1 with `ok: false` and
`admit: false`. Read `admit`; treat anything but `true` as refuse.

## What a refusal means

New work does not start. Work already admitted drains to its branches; its
merge is refused by the ruleset itself (the enforced layer). A refusal names
its code, so an operator can tell "halted" from "GitHub unreadable".

## vectors.json

`{"schema_version": 1, "vectors": [...], "origin_vectors": [{origin, github}]}`;
each of `vectors` is `{name, listing, detail, admit, code}` —
`listing` / `detail` are what the two reads returned (`null` = unreadable or
not read); `admit` / `code` are the required answer.
