---
spec_stage: requirements
status: draft
version: 1
generated_by: claude@claude-opus-4-8
generated_at: 2026-07-05
source_prompt_version: sha256:pending
validation: pending
approved_by: null
approved_at: null
---

# C4: Maestro decomposer → делегирование spec-runner — Requirements

> Снимает существующий дубль формата (consolidation-ADR). Владелец: Maestro. Consulted:
> spec-runner, steward. Upstream: спека C1 полезна, но `plan --full` существует уже сейчас —
> жёсткого блока нет.

## Контекст (реальный код)

- `Maestro/maestro/decomposer.py:85` — `SPEC_GENERATION_PROMPT`: **встроенная копия** формата
  `tasks.md` («spec-runner parses this EXACT format»).
- `decomposer.py:327` `generate_spec()` — строит промпт, зовёт `self._run_claude(prompt)`, пишет
  `spec/tasks.md` напрямую (комментарий: «prompt generates only tasks.md»).
- `decomposer.py` `_write_spec_files()` — marker-парсер, похоже неиспользуемый альтернативный путь.
- spec-runner уже умеет авторинг: `plan --full` (cli.py:953, cli_plan.py:268) генерит
  requirements/design/tasks из описания через `build_generation_prompt` (SSOT формата).
- Maestro уже спавнит spec-runner субпроцессом: `cmd=["spec-runner","run","--all"]`
  (orchestrator.py:363, `create_subprocess_exec`).

## Requirements

#### REQ-501: generate_spec делегирует spec-runner
**Priority**: 🔴 P0
**Description**: `generate_spec()` порождает спеку вызовом `spec-runner plan` (субпроцесс) в
директории workstream'а, а не встроенным промптом.
**Acceptance Criteria**:
- [ ] `generate_spec` вызывает `spec-runner plan …` вместо `_run_claude(SPEC_GENERATION_PROMPT…)`
- [ ] Результат пишется в `workspace/spec/` (как сейчас)

#### REQ-502: Убрать дубль формата
**Priority**: 🔴 P0
**Rationale**: Один владелец формата — spec-runner.
**Description**: Удалить `SPEC_GENERATION_PROMPT` и встроенное описание формата из decomposer.
**Acceptance Criteria**:
- [ ] `SPEC_GENERATION_PROMPT` удалён
- [ ] В decomposer не осталось описания формата `tasks.md`

#### REQ-503: Режим авторинга — `--full`, не `--gated`
**Priority**: 🔴 P0
**Rationale**: Декомпозиция Maestro — автоматический параллельный флоу; человеческий гейт живёт
ВЫШЕ (в steward), а не внутри Maestro.
**Description**: Использовать `spec-runner plan --full` (авто req/design/tasks, без аппрув-гейтов).
**Acceptance Criteria**:
- [ ] Вызов без `--gated`; спека генерится без человеческого чекпоинта
- [ ] Совпадает с обещанием докстринга `generate_spec` (req+design+tasks)

#### REQ-504: Проброс workstream как описания
**Priority**: 🟠 P1
**Description**: title/description/scope workstream'а передаются как description/context в `plan`.
**Acceptance Criteria**:
- [ ] Описание собирается из полей `WorkstreamConfig`
- [ ] Scope доводится до spec-runner (context)

#### REQ-505: Версионный пин покрывает авторинг
**Priority**: 🟠 P1
**Description**: Пин spec-runner в `maestro/spec_runner.py` расширить на контракт `plan`-авторинга,
не только state-reader.
**Acceptance Criteria**:
- [ ] Пин-комментарий упоминает authoring-контракт
- [ ] Несовместимая версия spec-runner → внятная ошибка

#### REQ-506: Обратная совместимость исполнения
**Priority**: 🔴 P0
**Description**: Downstream не меняется.
**Acceptance Criteria**:
- [ ] `spec-runner run --all` исполняет сгенерированную спеку как прежде
- [ ] Флоу orchestrator (spawn/scope/PR) без изменений
- [ ] Существующие тесты Maestro зелёные (mock spec-runner-вызова)

#### REQ-507: Чистка мёртвого кода
**Priority**: 🟢 P3
**Description**: Убрать/пометить `_write_spec_files`, если не используется; `_run_claude`
сохранить, если ещё нужен `decompose()`.
**Acceptance Criteria**:
- [ ] Проверено использование `_write_spec_files`/`_run_claude`; неиспользуемое удалено
