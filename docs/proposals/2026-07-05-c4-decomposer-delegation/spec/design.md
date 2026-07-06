---
spec_stage: design
status: draft
version: 1
generated_by: claude@claude-opus-4-8
generated_at: 2026-07-05
source_prompt_version: sha256:pending
validation: pending
approved_by: null
approved_at: null
---

# C4: decomposer → делегирование — Technical Design

## Design Principles

### DESIGN-501: Новое тело generate_spec
Собрать описание из `WorkstreamConfig` и вызвать spec-runner субпроцессом с `cwd=workspace_path`,
чтобы `spec/` лёг на месте. Паттерн — как существующий спавн в orchestrator.py:363
(`create_subprocess_exec`/`subprocess.run`).
```
desc = f"{workstream.title}\n\n{workstream.description}\n\nScope: {', '.join(workstream.scope)}"
cmd = ["spec-runner", "plan", "--full", "--from-file", <tmp desc>]   # или --description
subprocess.run(cmd, cwd=workspace_path, check=True)
```
Трасса: REQ-501, REQ-504.

### DESIGN-502: Контракт вызова
`spec-runner plan --full` пишет `spec/{requirements,design,tasks}.md`. Maestro потребляет
`tasks.md` как раньше (downstream `run --all` не меняется). Формат — целиком у spec-runner.
Трасса: REQ-502, REQ-506.

### DESIGN-503: Режим — --full (не gated)
`--gated` требует человеческих аппрувов → несовместимо с автоматическим параллельным флоу Maestro.
Governance-гейтинг — обязанность steward НАД Maestro, не внутри декомпозиции. Поэтому `--full`.
Трасса: REQ-503.

### DESIGN-504: Ошибки
Ненулевой exit `spec-runner plan` → `DecomposerError` (как сейчас при провале генерации).
Логировать stderr. Трасса: REQ-501.

### DESIGN-505: Пин версии
`maestro/spec_runner.py` (integration boundary, pinned version) — дополнить: пин покрывает
authoring-контракт `plan --full` (наличие флага, раскладка `spec/`). При несовпадении версии —
внятная ошибка на старте, не в середине прогона. Дисциплина та же, что для state-reader/`obs.py`.
Трасса: REQ-505.

### DESIGN-506: Чистка
Удалить `SPEC_GENERATION_PROMPT`. Проверить графом вызовов: `_write_spec_files` (marker-парсер) —
если больше не зовётся, удалить; `_run_claude` — сохранить, если используется `decompose()`
(декомпозиция описания в workstreams всё ещё через Claude CLI). Трасса: REQ-502, REQ-507.

## Точки изменения (файлы)
| Файл | Что меняется |
|---|---|
| `maestro/decomposer.py` | `generate_spec` → subprocess `spec-runner plan --full`; удалить `SPEC_GENERATION_PROMPT`; чистка `_write_spec_files` |
| `maestro/spec_runner.py` | пин расширить на authoring-контракт |
| `tests/test_decomposer*.py` | мокать subprocess-вызов вместо `_run_claude`; golden на разбор результата |

## Взаимодействие с C1
`plan --full` после C1 получит `--profile` — необязательно для C4. C4 работает на текущем
`--full`; при желании позже добавить `--profile lite`. Не блок.

## Compatibility / Migration
Изменение локально в decomposer + пин. Orchestrator, workspace, PR-флоу, downstream `run --all` —
без изменений (REQ-506). Риск №1 — регресс генерации: закрывается golden-тестом (DESIGN → tasks.md
парсится `task.py` spec-runner).
