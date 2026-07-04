---
name: idea-workstream-framework
description: "Future-direction idea — framework/tooling for authoring Maestro workstreams (scaffold + validate → SDK → maybe DSL) + import from other formats"
metadata:
  type: project
  raised: 2026-07-03
  status: direction-to-evaluate (not scheduled work)
---

## Проблема

Mode-2 `project.yaml` правится руками как голый YAML: нет `init`/scaffold-команды,
единственный шаблон — `examples/project.yaml`, а де-факто схема — pydantic
`ProjectConfig` / `WorkstreamConfig` (`maestro/models.py:753,957`). Авторинг
workstream'ов держится на знании схемы в голове и копипасте примера.

## Направление (по возврату на усилие, от дешёвого к дорогому)

1. **Scaffold + validate (первый шаг, максимальный ROI).**
   - `maestro init` — генерит `project.yaml` из pydantic-схемы.
   - `maestro validate` — scope/DAG-проверки: single-owner, glob-overlap между
     workstream'ами, циклы в `depends_on`, хорошие сообщения об ошибках.
   Закрывает ~90% боли «голого YAML» малой кровью.

2. **SDK** — программная сборка + валидация workstream'ов (не только YAML).
   Строить только если после scaffold+validate осталась реальная боль.

3. **DSL** — отдельный язык поверх YAML. Почти наверняка преждевременно: платишь
   документацией, миграциями, обучением ради эргономики, которую на 80% решает
   генератор + валидатор. Не начинать без явной причины.

4. **Import from other format** — конвертация внешнего представления задач в
   workstream'ы. Отложить до момента, когда назван конкретный формат-источник,
   который реально хочется импортировать.

## Scope-linter: что переиспользовать (OSS-скан 2026-07-03)

Готового «workstream scope linter» не существует. CODEOWNERS-экосистема +
repo-линтеры дают ~80% кирпичей: codeowners-validator (file→owner + синтаксис),
repolinter / MegaLinter (движок правил). Строгий single-owner + `depends_on` DAG —
специфика мульти-агентных оркестраторов вроде Maestro, поэтому этот кусок — bespoke.

## Смежное

- **appgraph** (root-repo прототип) концептуально пересекается с DAG-планировщиком
  Maestro — риск дублирования; сверить прежде чем строить graph-часть здесь.
- **deployer** может стать workstream/spawner-типом поверх этой же схемы, а не
  отдельной вселенной (см. `deployer/docs/idea-deployer-subproject.md`).

## Рекомендуемое первое действие

Сделать `maestro init` + `maestro validate` как узкий вертикальный срез на уже
существующих pydantic-контрактах. SDK/DSL/import — за гейтом «доказанной боли».
