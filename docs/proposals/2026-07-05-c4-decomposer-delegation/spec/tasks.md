---
spec_stage: tasks
status: draft
version: 1
generated_by: claude@claude-opus-4-8
generated_at: 2026-07-05
source_prompt_version: sha256:pending
validation: pending
approved_by: null
approved_at: null
---

# C4: decomposer → делегирование — Tasks

> Priority: 🔴 P0 · 🟠 P1 · 🟢 P3 | Status: ⬜ TODO · 🔄 IN PROGRESS · ✅ DONE · ⏸️ BLOCKED
> Инвариант: downstream `run --all` и тесты Maestro зелёные на каждом шаге (REQ-506).

---

## Milestone 1: Делегирование

### TASK-501: generate_spec → subprocess spec-runner plan --full
🔴 P0 | ⬜ TODO | Est: 3-4h

**Checklist:**
- [ ] Собрать описание из `WorkstreamConfig` (title/description/scope)
- [ ] `subprocess.run(["spec-runner","plan","--full",…], cwd=workspace_path, check=True)`
- [ ] Результат в `workspace/spec/`; ненулевой exit → `DecomposerError` (+ stderr в лог)
- [ ] Юнит-тест: мок subprocess, проверка команды и cwd

**Traces to:** [REQ-501], [REQ-503], [REQ-504], [DESIGN-501], [DESIGN-503], [DESIGN-504]
**Depends on:** -
**Blocks:** [TASK-502], [TASK-504]

---

## Milestone 2: Снять дубль

### TASK-502: Удалить SPEC_GENERATION_PROMPT + чистка
🔴 P0 | ⬜ TODO | Est: 1-2h

**Checklist:**
- [ ] Удалить `SPEC_GENERATION_PROMPT` (decomposer.py:85)
- [ ] Граф вызовов: `_write_spec_files` — удалить, если мёртв
- [ ] `_run_claude` — сохранить, если нужен `decompose()`; иначе убрать
- [ ] Убедиться: в decomposer нет описания формата `tasks.md`

**Traces to:** [REQ-502], [REQ-507], [DESIGN-506]
**Depends on:** [TASK-501]
**Blocks:** [TASK-504]

---

## Milestone 3: Пин контракта

### TASK-503: Расширить пин в spec_runner.py
🟠 P1 | ⬜ TODO | Est: 1-2h

**Checklist:**
- [ ] Пин-комментарий в `maestro/spec_runner.py` покрывает `plan --full` authoring-контракт
- [ ] Несовместимая версия spec-runner → внятная ошибка на старте
- [ ] Тест на проверку версии

**Traces to:** [REQ-505], [DESIGN-505]
**Depends on:** -
**Blocks:** [TASK-504]

---

## Milestone 4: Верификация

### TASK-504: verification — zero regression
🔴 P0 | ⬜ TODO | Est: 2-3h

**Description:**
Подтвердить, что делегирование не ломает orchestrator-флоу и снимает дубль.

**Checklist:**
- [ ] Golden: `spec-runner plan --full` на фикстуре workstream → `tasks.md` парсится `task.py`
      spec-runner (контракт формата держится без встроенной копии)
- [ ] E2E orchestrator (mock spec-runner): spawn/scope/PR-флоу без изменений
- [ ] Полный тест-сьют Maestro зелёный
- [ ] grep-проверка: `SPEC_GENERATION_PROMPT` отсутствует в репо

**Traces to:** [REQ-502], [REQ-506]
**Depends on:** [TASK-502], [TASK-503]
**Blocks:** -
