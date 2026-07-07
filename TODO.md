# TODO — Maestro (план от 2026-04-16, snapshot 2026-04-25)

> Стратегический контекст: `../_cowork_output/roadmap/ecosystem-roadmap.md`
> Последние недельные отчёты: `../_cowork_output/status/2026-04-24-status.md`, `2026-04-18-status.md`, `2026-04-10-status.md`
> Критический путь: ✅ закрыт (R-01..R-04 shipped в v0.2.0, observability M1+M2 закрыты, arbiter#9 фикс 2026-04-25)

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- Если задача стала неактуальной — зачеркни `~~...~~` с пометкой **почему**
- Не добавляй новые задачи без обновления roadmap в `_cowork_output/`

---

## День 1 — разблокировка (parallel, effort S)

- [x] **R-01: Нормализация agent IDs** — `codex` → `codex_cli` (commit `8fd0b51`)
  - `maestro/models.py:76` — `CODEX = "codex"` → `CODEX = "codex_cli"`
  - Затронутые файлы (grep уже сделан): `models.py`, `cost_tracker.py`, `spawners/codex.py`, `schemas/project_config.json`, `executor.config.yaml`, `tests/test_models.py`, `tests/test_cost_tracker.py`, `tests/test_spawners.py`, `tests/test_spawner_registry.py`
  - Мотивация: arbiter в `config/agents.toml` использует `codex_cli`, без этого R-03 вернёт reject на первом вызове
  - Verify: `uv run pytest && uv run pyrefly check`
  - Примечание при выполнении: `executor.config.yaml` и `shutil.which("codex")` / `Popen(["codex", ...])` не менялись — там фигурирует имя CLI-бинарника, а не enum‑идентификатор. `test_cost_tracker.py` менять не потребовалось: тесты используют `AgentType.CODEX` (имя константы сохранилось, изменилось только `.value`). Regen: `uv run python -m maestro.schemas.generate`. Результат: 953/953 pytest, pyrefly clean, ruff clean.

- [x] **R-09: GitHub Actions CI** — pytest + ruff + pyrefly (commits `36a1671` → `5e66357` → `05e5089`, run `24492556426` green)
  - Создать `.github/workflows/ci.yml`
  - Образец: `../spec-runner/.github/workflows/ci.yml` (заменить `mypy src` на `pyrefly check`, trigger: push на `main` + PR)
  - Matrix: Python 3.12+ (из pyproject.toml)
  - Мотивация: 29 тестов запускаются только вручную, ежедневные коммиты без safety net — блокер для open-source v0.1.0
  - Примечание: 3 job'а (lint / typecheck / test на py3.12+3.13), trigger — push на `master` + PR (фактический branch у проекта — master). Попутно применён `ruff format` к `maestro/cli.py` (pre-existing mismatch). Первый прогон вскрыл 22 pre-existing фейла, исправленных настройкой runner-а: `git config init.defaultBranch main` + `user.email`/`user.name` (тесты `test_git*` создают temp repos и делают `checkout main`/merge); `TERM=dumb` для теста (GitHub Actions форсит `FORCE_COLOR=1`, Rich игнорирует `NO_COLOR` для bold/dim, из-за чего help-строки вида `--resume` разбивались ANSI-кодами). Финальный прогон: 952 passed, 1 slow deselected, все 4 job'а green. Node.js 20 deprecation warnings (action versions) — non-blocking, можно обновить потом.

- [x] **R-08: Пометить неработающие интеграции в корневом COWORK_CONTEXT.md** (не в git)
  - Файл: `../COWORK_CONTEXT.md` (вне Maestro, но задача туда)
  - Maestro→Arbiter и Maestro→ATP помечены как существующие — это вводит в заблуждение
  - Проставить `🔴 NOT IMPLEMENTED` или `⚠️ PLANNED` рядом со стрелками
  - Сделано: `⚠️ PLANNED` заменён на `🔴 NOT IMPLEMENTED` в диаграмме интеграций для Maestro→Arbiter и Maestro→ATP. Секция «Контрактные точки → Maestro ↔ Arbiter (MCP)» получила жирный заголовок `🔴 NOT IMPLEMENTED` + disclaimer с разблокирующими R-01/R-02/R-03. Обновлён таймстемп `Последнее обновление` на 2026-04-16. Parent-директория не git-репо, коммитить некуда — изменения на диске.

- [x] **R-06a: Пример `validation_cmd: "atp test ..."`** (quick win, 0 строк кода) (commit `5c4c25f`)
  - Файл: `examples/tasks.yaml` или новый `examples/with-atp-validation.yaml`
  - Показать, как `validator.py` запускает ATP CLI после задачи
  - Мотивация: открывает доступ к ATP-оценке без ожидания R-03
  - Сделано: `examples/with-atp-validation.yaml` (88 строк). 3 паттерна: (1) pytest + ATP через `&&`; (2) ATP-only для задач без unit-тестов + JSON artifact для retry; (3) `--tags=smoke` для быстрых повторов. Маппинг exit-кодов ATP (0/1/2) на Maestro state machine задокументирован в заголовке. Валидация: `maestro.config.load_config` парсит все 3 `validation_cmd` корректно. Примечание: команда ATP CLI — `atp test`, не `atp run` (как было в TODO).

---

## Неделя 2 — формализация (effort M)

- [x] **R-04: ExecutorState Pydantic-модель** (commits `0498c82` + `cc9ee02`, CI run `24494341902` green)
  - Сейчас `.executor-state.json` парсится как dict в `maestro/orchestrator.py` и `maestro/workspace.py`
  - Создать `ExecutorState` в `maestro/models.py` (рядом с `Task`, `Workstream`)
  - Зафиксировать версию `spec-runner` в `pyproject.toml`
  - Добавить contract test: Maestro генерирует конфиг → spec-runner его парсит
  - Мотивация: единственная работающая интеграция держится на неформальном контракте, ломается при любом обновлении spec-runner
  - Сделано: 4 типизированные модели (`ExecutorState`/`ExecutorTaskEntry`/`ExecutorTaskAttempt`/`ExecutorTaskStatus`) с `extra="ignore"` для форвард-совместимости. Новый модуль `maestro/spec_runner.py` — integration boundary: константа `SPEC_RUNNER_REQUIRED_VERSION="2.0.0"`, helper `read_executor_state(spec_dir)` с приоритетом SQLite (read-only `file:?mode=ro` URI — не блокирует writer'а) + fallback JSON, детектом опциональных колонок через `PRAGMA table_info`. **Побочный баг-фикс:** `orchestrator._update_progress` читал stale `.executor-state.json`, которого нет в spec-runner 2.0 — progress в дашборде и БД молча стоял. Теперь через `read_executor_state` работает и с SQLite. +11 contract-тестов (1010 всего): version pin, JSON parsing + unknown fields + malformed, SQLite real schema, SQLite-beats-JSON, corrupt-SQLite fallback, `to_executor_config()` shape, round-trip + invalid status rejection.

---

## Недели 3+ — критическая цепочка интеграции (effort M → L)

- [x] **R-02: Расширение TaskConfig полями Arbiter** (commit `8a3cba8`, CI run `24493970314` green)
  - `maestro/models.py:81-154` (`Task`/`TaskConfig`)
  - Добавить required поля: `task_type` (7 enum), `language` (6 enum), `complexity` (5 enum)
  - Маппинг `priority`: int(-100..100) → enum(low/normal/high/urgent)
    - `-100..-26` → `low`, `-25..25` → `normal`, `26..75` → `high`, `76..100` → `urgent`
  - Опциональная автоинференция: `language` из scope (`*.py`→python, `*.rs`→rust), `task_type` из prompt (ключевые слова: "fix"→bugfix, "test"→test)
  - Reference: `arbiter-core/src/types.rs`
  - Сделано: 4 StrEnum (`TaskType`/`Language`/`Complexity`/`Priority`) в snake_case под arbiter. Поля в `TaskConfig` — optional (auto-inference через `infer_task_type`/`infer_language`/`infer_complexity` в `Task.from_config`), в `Task` — required с дефолтами (feature/other/moderate) для обратной совместимости с прямым конструированием в тестах/scheduler. Приоритет остался `int` + helper `priority_int_to_enum(int)`. DB миграция: ALTER TABLE для pre-R-02 схемы через `_migrate_tasks_arbiter_columns` (использует `PRAGMA table_info` для идемпотентности). +46 тестов (999 всего). Регенерирована `project_config.json`. Дальше — R-03 (MCP-клиент), используем `priority_int_to_enum` и enum-поля напрямую на payload.

- [x] **R-03: MCP-клиент Arbiter в Maestro** (ветка `feat/r-03-arbiter-client`, 16 коммитов `ba8b950..80b7a2f`)
  - Новые модули: `maestro/coordination/arbiter_client.py` (vendored от arbiter@`861534e`), `maestro/coordination/routing.py` (`StaticRouting` + `ArbiterRouting` + `task_status_to_outcome_status` + `make_routing_strategy` фабрика), `maestro/coordination/arbiter_errors.py`
  - Модели: `AgentType.AUTO`, `ArbiterConfig`, `ArbiterMode`, `RouteAction`, `RouteDecision`, `TaskOutcome`, `TaskOutcomeStatus`; Task получил `routed_agent_type`/`arbiter_decision_id`/`arbiter_route_reason`/`arbiter_outcome_reported_at`
  - Scheduler: `_spawn_task` советуется с routing → ASSIGN/HOLD/REJECT; `_handle_task_completion`/`_handle_task_failure` доставляют outcome; mode-aware retry gating через `reset_for_retry_atomic` с decision_id guard; `_outcome_reattempt_pass` в main loop (bounded 5/tick) с authoritative abandon timer
  - Recovery: `recover_arbiter_outcomes()` закрывает висящие решения после краша, интегрировано в `StateRecovery.recover(routing=…)`
  - CLI: `maestro run` читает `ProjectConfig.arbiter`, строит routing через `make_routing_strategy`, плюмит `arbiter_enabled`, закрывает subprocess в `finally`
  - Event log: 10 новых `EventType` (ARBITER_ROUTE_DECIDED/HOLD/REJECTED/HOLD_SUMMARY/OUTCOME_REPORTED/OUTCOME_ABANDONED/UNAVAILABLE/RECONNECTED/RETRY_RESET_SKIPPED + RECOVERY_ARBITER_DECISIONS_CLOSED), `HoldThrottle` helper
  - DB: 4 новых колонки на `tasks` + миграция + `update_task_routing` / `mark_outcome_reported` / `reset_for_retry_atomic` / `get_tasks_with_pending_outcome` / `abandon_pending_outcome_and_release`
  - Тесты: +113 новых (1112/1112), pyrefly clean, `ruff check .` clean, `ruff format --check .` clean
  - Пример: `examples/with-arbiter.yaml` (смоук-проверен через `maestro.config.load_config`); `examples/tasks.yaml` — arbiter=None, zero-config путь не задет
  - Pending manual acceptance (требует локальной сборки arbiter-mcp): (a) advisory + kill arbiter → retry всё равно идёт; (b) authoritative + kill, < abandon_outcome_after_s → FAILED держится; (c) authoritative + kill, > abandon_outcome_after_s → `arbiter.outcome.abandoned` событие + unblock

### Follow-ups разблокированные R-03

Дальнейший трек ведётся в Linear (Maestro / Arbiter проекты, team Labs). Ниже — snapshot на 2026-04-17.

- [ ] **R-03b** (LABS-TBD): Mode 2 (`maestro orchestrate`) workstream-level routing. Gate: ≥1 неделя стабильного Mode-1 dogfood после v0.2.0
- [x] **R-05 contract-level** (commit `f1f7d26`, 2026-04-25): 4 e2e теста против реального `arbiter-mcp` бинарника в `tests/test_arbiter_real_subprocess.py`. Auto-skip без бинарника; `MAESTRO_ARBITER_BIN` override. Покрывает: decision_id i64, int→str coercion, route→report_outcome round-trip, distinct rowids.
- [x] **R-05 CI job** (2026-05-07): новый `arbiter-e2e` job в `.github/workflows/ci.yml` — sibling-checkout Maestro + arbiter (`andrei-shtanakov/arbiter`), `cargo build --release --bin arbiter-mcp` под Swatinem cache, прогон `tests/test_arbiter_real_subprocess.py` с `MAESTRO_ARBITER_BIN`. Ref-strategy: PR/push на pinned `ARBITER_PINNED_SHA=d1a8ecd` (arbiter#9 fix), weekly schedule (Mon 06:00 UTC) на `master` для drift-check. Локальный smoke: 4/4 теста зелёные.
- [x] **R-05 scheduler-driven e2e** (2026-05-07): `tests/test_scheduler_arbiter_real_subprocess.py` — 2 теста скрещивают real arbiter-mcp + Scheduler full cycle + MagicMock spawner. (1) ASSIGN happy-path: real arbiter routes → mock exit 0 → outcome reported back to real arbiter → DONE; проверяет int→str round-trip decision_id через TEXT-колонку. (2) Retry-gating с real rowids: exit 1 → ADVISORY reset → второй route real arbiter mint'ит fresh i64 ≠ первого. HOLD/REJECT покрыты в `test_scheduler_arbiter_integration.py` через FakeArbiter — дублирование через real subprocess не оправдано (требует seed'инга cost/failure history)
- [x] **arbiter#9 client-side fix** (commit `e5915f2`, 2026-04-25): `_extract_decision_id` коэрсит `int → str` для `arbiter_decision_id TEXT` колонки и stale-guard. Парная с arbiter `d1a8ecd`. 8 unit-тестов в `TestExtractDecisionId`
- [x] **R-10** (LABS-91 / arbiter#8, `7e6de56`): Arbiter CI release-binary. Готово: linux-x64 + macos-arm64 30-day artifacts. Открыто: tag-triggered GitHub Release upload, `pyrefly check` в Python job
- [x] **R-NN** (LABS-84, commit `ab279f2`): wire `cost_tracker` в `Scheduler._record_cost`. `TaskOutcome.tokens_used` / `cost_usd` теперь несут реальные значения. Model variants / structured usage — отдельно под LABS-49
- [x] **Mini-R** (LABS-85, commit `627c12d`): `schema_migrations` journal + линейный migration runner. Добавление миграции #3+ = одна строка в `ordered` + метод
- [ ] **R-14**: Вынести vendored `arbiter_client.py` в отдельный PyPI-пакет `arbiter-py` (upstream arbiter work, не в Linear пока)

### R-06b — Agent benchmarking via ATP

> Дизайн: `../_cowork_output/decisions/2026-04-25-r06b-design.md`
> M0 (design) approved by virtue of M1 landing.

- [x] **R-06b M1 thin slice** (2026-05-07): новый `maestro/benchmark/` модуль — `BenchmarkRunner` + Protocols (`ATPClientLike`, `BenchmarkRun`, `AgentResponder`), Pydantic-модели (`BenchmarkResult`, `BenchmarkTaskResult`, `AgentResponse`). Async API (Maestro async-first; M2 spawner и M3 ATP HTTP-клиент будут async). Mock-only тесты в `tests/test_benchmark_runner.py` — 2 кейса: happy path с агрегацией tokens/cost и agent-error path (None ≠ 0 для отсутствия измерений). Цель M1 достигнута: API shape залочен, M2..M5 могут идти параллельно
- [x] **R-06b M2 spawner integration** (2026-05-08): `maestro/benchmark/spawner_responder.py` — `SpawnerResponder` обёртывает любой `AgentSpawner` (claude_code/codex_cli/aider) и реализует `AgentResponder`. Синтез минимального `Task` под benchmark prompt, `asyncio.to_thread(process.wait)` под `asyncio.wait_for(timeout)`, парсинг tokens/cost через существующий `cost_tracker.{parse_log,calculate_cost}` (без db side-effects). `response.text` = full log content (M2 punt; M3 уточнит per-benchmark extraction). +4 теста в `tests/test_spawner_responder.py`: happy path, timeout (kill + unblock), non-zero exit, unknown agent_type short-circuit
- [x] **R-06b M3 auth + live ATP** (2026-05-08): новый `maestro/benchmark/atp_client.py` — `MaestroATPAdapter` оборачивает `atp_sdk.AsyncATPClient` (PyPI `atp-platform-sdk>=2.0.0`) под M1 Protocols. Auth UX делегирован SDK: token resolution `explicit → ATP_TOKEN env → ~/.atp/config.json` (Device Flow encapsulated в SDK, Maestro его не дублирует). Конструкторы `from_env`/`from_token`. Bridge-перевод: `run_id: int → str`, raw ATPRequest dict → typed `_Task` (вытащены `metadata.task_index` + `task.description` + `task_id`), submit оборачивает `response: str` в ATPResponse (`status="completed"|"failed"` по непустоте, текст в `ArtifactStructured`), `finalize()` делает GET `/runs/{id}/status` и читает `total_score`. `score_components={}` пока ATP не экспортирует breakdown. +6 тестов через monkeypatch `AsyncATPClient._request` (`FakeRequestQueue`): auth headers, env fallback, run_id-cast, end-to-end iteration с проверкой ATPResponse shape + task_id reuse, failed-status path, finalize при отсутствии total_score. 1156/1156 pytest, pyrefly clean, ruff clean
- [x] **R-06b M4** (2026-05-23, merged via PRs #19/#20/#21, last merge SHA `5edb359`; main M4 merge `3066ded`): new MCP tool `report_benchmark` in arbiter-mcp + `maestro/benchmark/arbiter_report.py` helper. Persist-only into new `benchmark_runs` table (single row + per_task jsonb); `INSERT...ON CONFLICT(run_id) DO NOTHING` idempotency; fire-and-forget emit with `BenchmarkResult.report_status`/`report_error` (immutable `model_copy`). Schema-first contract in `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json`. Vendored client `MIN_ARBITER_PROTOCOL=(1,1)` + `ARBITER_VENDORED_FROM_SHA` pin + CI drift check. New typed `ArbiterContractError` differentiates JSON-RPC contract breaks (-32600/-32602/-32603) from transient `ArbiterUnavailable`. 5 distinct obs events (`benchmark.report.{skipped,succeeded,duplicate,failed,contract_break}`); contract_break gets ERROR severity. Smoke script `scripts/smoke_benchmark_report.py` + 3-case e2e in `arbiter-e2e` CI job (created/duplicate/contract_break). Arbiter Phase 1: merged via PR #11 at SHA `7aeb6b1`; subsequent hardening via PRs #13/#14/#15 (latest arbiter master `81fe183`). Recommended minimum SHA for full feature: `151004b` (PR #13). Full design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md` + plan `docs/superpowers/plans/2026-05-23-r06b-m4-arbiter-wiring.md`.
- [x] **R-06b M5 CLI**: `maestro benchmark <benchmark-id> --agent claude_code` (closed by feat/benchmark-cli)

### Follow-ups from R-06b M4

- [ ] **M3-obs / arbiter trace**: W3C `traceparent` injection in MCP JSON-RPC envelope (spans all arbiter calls, not specific to M4). Trigger: when `benchmark.report.*` events need correlation with arbiter-side INSERT by `trace_id`. Severity: medium.
- [ ] **R-06b M4b**: revisit `max_per_task=200` sampling for swe-bench-full (>1000 tasks). Trigger: first PROD swe-bench-full run.
- [ ] **R-07 prereq (GIN index)**: GIN index on `benchmark_runs.per_task` jsonb. Trigger: when R-07 starts writing SQL filters on per_task.
- [ ] **R-07 prereq (normalize)**: normalize `benchmark_task_results` table (migration from jsonb blob). Trigger: same as GIN — formal query demand.
- [ ] **R-07 prereq (retention)**: TTL / archive policy for `benchmark_runs`. Trigger: table > 10k rows OR > 1 GB total JSON blobs.
- [ ] **R-14**: vendored `arbiter_client.py` → standalone PyPI `arbiter-py` package. M4 enlarged vendor surface.
- [ ] **Unscheduled — outbox**: persistent outbox + background retry for benchmark report. Trigger: if fire-and-forget shows real CI churn.
- [ ] **Unscheduled — arbiter-initiated benchmark**: outgoing benchmark trigger from arbiter ("router uncertain → run benchmark"). From design open question #2.
- [ ] **M5 / multi-tenant auth**: service-account ATP token for CI; multi-tenant arbiter auth as separate ticket if arbiter ever leaves subprocess trust model.

### Новое из v0.2.0 dogfood (LABS-87..90)

- [x] **LABS-87** (2026-05-07): validation-failure path теперь репортит outcome в arbiter с retry-gating. `_handle_validation_failure` отзеркалил `_handle_task_failure`: build outcome (status FAILURE) → `_try_report_outcome` → ADVISORY/AUTHORITATIVE-aware reset. Both paths (retry-available + exhausted-NEEDS_REVIEW) шлют outcome. +4 теста в `test_scheduler_arbiter_integration.py` (advisory+retry, exhausted, advisory+arbiter-down, authoritative+arbiter-down). Routing API не расширен — `validation_passed` остаётся out-of-scope
- [ ] **LABS-88** (Low): CI guard для unreferenced public modules
- [ ] **LABS-89** (Medium): release automation (version-vs-tag guard + release-drafter)
- [ ] **LABS-90** (Medium): per-example YAML smoke test в CI

### Observability (cross-project) — M1 closed, M2 closed 2026-04-25

- [x] **M1** (commits `e3feefd`, `4688633`, `279193e`): cross-process trace continuity. Vendored `obs.py` от spec-runner@`fa6b106`, contract в `_cowork_output/observability-contract/` (log-schema, propagation, 4 fixtures), CLI `init_logging("maestro")`, child_env() пропагация в orchestrator
- [x] **M2** (commit `d474120`, 2026-04-25): scheduler instrumentation. `obs.span("scheduler.session")` + `obs.span("task.spawn")` (subprocess inheritance через TRACEPARENT), 4 структурированных emit'а (`task.completed`/`task.validation_failed`/`task.failed`/`task.timeout`), `spawn_env()` helper в `spawners/base.py` пропагирует трасу в claude_code/codex/aider/validator subprocesses. 3 теста в `test_scheduler_observability.py`
- [ ] **M3** (pending): scheduler-tick instrumentation (per-poll-cycle metrics), arbiter routing decision span, observability dashboards

---

## C4 — Decomposer delegation

- [x] **Delegate spec generation to spec-runner plan --full** (closed by feat/c4-decomposer-delegation): spec-runner owns the tasks.md format; removed SPEC_GENERATION_PROMPT and _write_spec_files.

---

## Чего НЕ делать до стабилизации

- ❌ Shared type library (R-14, XL) — преждевременно, сначала зафиксировать схемы
- ❌ `agent-infra.yaml` декларативная конфигурация (R-15, XL)
- ❌ Monorepo vs multi-repo решение (R-16, XL)

---

## Как проверить факт выполнения

Все задачи кросс-проектные — их «готовность» проверяется конкретными grep/ls (образец в `~/.claude/projects/.../memory/roadmap-status-2026-04-16.md`). После R-01/R-02/R-03 прогнать:

```bash
# R-01
grep -rn "codex_cli\|\"codex\"" maestro/ tests/
# R-02
grep -n "task_type\|complexity\|language" maestro/models.py
# R-03
grep -rn "arbiter\|route_task\|ArbiterClient" maestro/
# R-09
ls .github/workflows/
```

---

## Catalog distribution follow-ups (ADR-ECO-003b)

- [ ] XDG default catalog path ($XDG_CONFIG_HOME/<eco>/agents-catalog.toml) once the
      <eco> namespace is ratified; extend `resolve_catalog_path`.
- [x] `maestro models init | list | discover | update` CLI (ADR-003b D3) (closed by feat/models-cli).
- [ ] Shared `CLAUDE_MODEL` / `CODEX_MODEL` cross-tool override layer.
- [ ] `default = true` field in the catalog `[[agents]]` schema to disambiguate the
      A/B window (cross-repo, PM-owned) — removes the `HarnessModelUnresolved`
      ambiguity raise.
- [ ] Extract the loader to a shared PyPI lib with a cross-reader behavioral
      conformance test (precedence + alias resolution across Maestro / ATP / arbiter).
- [ ] `maestro models`: detect the same observed model id under TWO vendors in
      one manifest — today it renders an unparseable Plane-1 block (two
      `[models."id"]` tables); update refuses safely via the validation gate
      (cryptic tomllib message), discover --out writes the broken block while
      exiting 2. Should become its own report category or fold into
      vendor_conflicts.

## opencode follow-ups (ADR-ECO-003c)

- [x] Cost-from-log: surface `part.cost` (and optionally cache_read/cache_write)
      from opencode JSONL into TaskCost/TaskOutcome instead of PRICING-based 0.
      Constraint (recorded in parse_opencode_log docstring): cache_read must
      NOT be billed at full input price — in real runs cache_read ~= input.
      Until then opencode reports cost_usd=None (unknown) to the arbiter.
      (closed by feat/cost-from-log)
- [x] opencode entry in the ecosystem SSOT catalog (atp-platform/method/
      agents-catalog.toml) — cross-repo; the test fixture already carries
      harness=opencode / glm-5.1.
      Verified 2026-07-05: atp-platform/method/agents-catalog.toml has
      [harnesses.opencode] + one routable [[agents]] opencode/glm-5.1
      (promoted 2026-07-03, gate 003a D4) + two Path B non-routable entries;
      Maestro's loader resolves default_model_for_harness('opencode') ==
      'glm-5.1' against it. Done upstream by the atp-platform actor.
- [x] Routed-path token telemetry: `parse_and_create_cost` keys the parser off
      the DECLARED `task.agent_type` (scheduler.py), so a task routed to
      opencode (`agent_type: auto`, or an authoritative arbiter override)
      never reaches `parse_opencode_log` — token usage is silently zero and
      the drift canary is bypassed. `cost_usd` stays None on that path, so
      router honesty holds; only the token signal is lost. Pre-existing
      structural gap (a claude→codex override mis-parses the same way).
      Fix alongside cost-from-log: dispatch the parser by EFFECTIVE harness
      (`harness_of_agent_id(task.routed_agent_type)` fallback) at the same
      call site.
      (closed by feat/cost-from-log)
- [ ] Recovery-path reported cost: `_reconstruct_outcome` (recovery.py) always
      reports cost_usd=None even when a persisted TaskCost row with
      reported_cost_usd exists for the crashed attempt — honest-unknown, but
      real dollars the DB already holds are lost on crash-recovery reports.
- [ ] Responder `cost or None` (spawner_responder.py) collapses a genuine
      reported $0.00 into None ("confirmed free" reads as "unknown") — becomes
      real when free/local open models run under opencode.
- [ ] Codex cost-from-log (research): `codex exec` writes plain text (no
      `--output-format json`); `parse_log` routes CODEX through the Claude JSON
      parser, which extracts nothing. Investigate whether codex can emit
      structured usage/cost (tokens + cost) and, if so, add a dedicated codex
      parser + `parse_log` route. (Deferred from the claude cost-from-log spec.)

- [ ] opencode parser: guard `part.cost >= 0.0` (parity with the claude cost
      guard). `parse_opencode_log` accepts a negative `part.cost`; a negative
      sum then fails `TaskCost.reported_cost_usd`'s `ge=0.0` validator and
      silently drops the whole row (tokens included) — the same silent-drop
      failure mode the NaN guard already prevents. The claude guard added
      `cost >= 0.0`; opencode's did not (so "guards mirror opencode exactly" is
      not literally true for the negative case). Low-probability (opencode is
      unlikely to emit a negative cost) but a real latent drop. (From the claude
      cost-from-log final review.)

- [x] Orchestrator startup recovery: workstreams stranded in DECOMPOSING or
      RUNNING after a hard crash are not re-resolved on `--resume`
      (`_resolve_ready` only picks PENDING/READY). Pre-existing; surfaced during
      C4 final review (Minor #4). Add crash-recovery re-resolution. (closed by feat/orchestrator-startup-recovery)
- [x] Orchestrator recovery follow-ups (from startup-recovery final review):
      (a) DECOMPOSING orphan liveness — record the `plan --full` generation pid
      so a stranded DECOMPOSING can be liveness-checked like RUNNING (today it
      re-decomposes blindly, could race an orphaned generation writing spec/).
      (closed by feat/decomposing-generation-pid-liveness)
      (b) Move `_merge_into_base` BEFORE the DONE transition (or add a
      merged-into-base check) so a crash during the base merge doesn't leave a
      workstream showing DONE with an unmerged feature branch.
      (closed by feat/base-merge-before-done)

- [ ] Uniform spawn→persist window closure (RUNNING + DECOMPOSING): a hard crash
      between spawning the subprocess and persisting its pid leaves status set
      with pid=NULL and a live orphan → recovery reads None → READY → re-run
      races the orphan. Close both windows symmetrically (e.g. a "spawning"
      sentinel pid recovery treats as "assume live → NEEDS_REVIEW"), including
      the already-merged RUNNING path. (From the gen-pid liveness spec's
      residual-risk section.) Fold in the parked-row cleanup: the recovery
      live-orphan branch leaves the stale pid (process_pid / generation_pid) on
      the NEEDS_REVIEW row — clear it for BOTH states together (harmless to
      recovery, but cleaner for REST/dashboard).
