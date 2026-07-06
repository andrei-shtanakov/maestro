# I1 · Maestro `validate` не ловит висячую `depends_on` между workstreams

> Тип: bug · Владелец: Maestro · Severity: low-medium · Найдено: 2026-07-05
> (при контракт-проверке emitter'а steward → `project.yaml`)
> Источник: `_cowork_output/spec-governance-dogfood/emitter-contract-check.md`

## Симптом

`maestro validate --no-fs` (и прямой `preflight.validate_project(check_fs=False)`) считает
конфиг валидным (`ok=True`, 0 issues), даже если workstream ссылается в `depends_on` на
**несуществующий** id.

## Репро

На валидном `project.yaml` (5 workstreams) добавить висячую зависимость:

```python
from maestro.config import load_orchestrator_config
from maestro.preflight import validate_project
p = load_orchestrator_config("project.yaml")
for w in p.workstreams:
    if w.id == "dispatcher-panel-dogfood":
        w.depends_on = list(w.depends_on) + ["does-not-exist"]
rep = validate_project(p, check_fs=False)
print(rep.ok, rep.issues)   # -> True []   ← ожидалось error/warning
```

Для сравнения (те же условия, детекторы работают):
- цикл `depends_on` → `error dag-cycle` ✅
- пересечение scope → `warning scope-overlap` ✅
- **висячая `depends_on` → ничего** ❌

## Ожидание vs факт

| | Ожидание | Факт |
|---|---|---|
| Висячая `depends_on` | `error` (id не существует) | `ok=True`, 0 issues |

## Предполагаемая причина

`preflight.validate_project` строит граф зависимостей для детекта циклов, но, судя по поведению,
**не проверяет, что каждый `depends_on`-id присутствует в множестве workstream-id**. Неизвестный
узел, вероятно, молча игнорируется обходом цикла.

## Предлагаемый фикс

В `validate_project` добавить проверку целостности рёбер: для каждого `w.depends_on` id ∈
{все workstream id}, иначе `ValidationIssue(severity="error", code="dangling-dep", ...)`.
Малый объём; статический, работает и в `--no-fs`.

## Влияние

- **Прямое:** битый `project.yaml` доходит до `orchestrate`, где ошибка всплывёт позже/тише.
- **Для steward (C4/E1):** emitter `decomposition → project.yaml` не может полагаться на
  `maestro validate` как на сеть безопасности для dep-целостности → `gate-check` steward
  проверяет межпоточные `depends_on` сам (учтено в WS-002 REQ-203). Фикс Maestro убирает
  дублирующую необходимость, но обе проверки полезны (defense-in-depth).

## Acceptance

- [ ] Висячая `depends_on` → `error` в `validate` (в т.ч. `--no-fs`)
- [ ] Регресс-тест на висячую зависимость
- [ ] Существующие тесты preflight зелёные

---

## Triage (Maestro, 2026-07-06)

Заявленный user-facing симптом **не воспроизводится**: висячая `depends_on`
ловится Pydantic-валидатором `OrchestratorConfig.validate_workstream_dependencies_exist`
(models.py:1365) на этапе `load_orchestrator_config` — ДО preflight. Проверено на
живом CLI: `maestro validate --no-fs` на YAML с `depends_on: [a, does-not-exist]`
даёт `Validation error … Workstream 'b' has unknown dependencies: {'does-not-exist'}`,
1 errors, exit 1. «Битый project.yaml доходит до orchestrate» — неверно: он не
проходит даже парсинг конфига.

Репро из отчёта обходит валидатор, МУТИРУЯ уже-провалидированный объект в памяти
(`w.depends_on = list(...) + ["does-not-exist"]` — присваивание поля не вызывает
re-validation). Реальный остаток бага: `preflight.validate_project` сам не
проверяет целостность рёбер, поэтому программные вызыватели, мутирующие конфиг
после загрузки (как emitter-контракт-чек steward'а), сетки безопасности не имеют.

Severity понижен: low (defense-in-depth для программного пути, не user-facing).
Фикс из отчёта (`dangling-dep` issue в validate_project) остаётся уместным и
дешёвым — но как hardening, не как баг CLI.

**Resolved** by `fix/preflight-dangling-dep` (commit 0e92a39): `_check_dangling_deps`
in preflight emits an `error` `dangling-dep` for unknown `depends_on` ids, covering
the programmatic mutate-after-load path.
