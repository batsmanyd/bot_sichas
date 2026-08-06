# Миграции и rollback

Production-миграции в рамках стабилизации не запускались.

## Перед staging

1. Остановить запись в staging.
2. Сделать проверяемый PostgreSQL backup (`pg_dump --format=custom`).
3. Проверить восстановление backup в отдельную БД.
4. Для существующей схемы 0.17.8 выполнить `alembic stamp 0001_baseline`.
5. Сгенерировать и просмотреть SQL: `alembic upgrade head --sql`.
6. Выполнить `alembic upgrade head` только на staging.
7. Проверить `alembic current`: ожидается `0007_runtime_guards (head)`.
8. Запустить smoke и concurrency tests.

Все команды требуют `DATABASE_URL`. Значения секретов нельзя помещать в
командную строку, логи или отчёты.

Production-приложение не выполняет `create_all()` и отказывается стартовать,
если `alembic_version` отсутствует или отличается от `0007_runtime_guards`.
Миграции запускаются отдельной командой с `ALEMBIC_RUNNING=1`.

## Rollback

1. Остановить приложение staging.
2. Сохранить новый forensic backup.
3. Для обратимой проверки использовать `alembic downgrade <revision>`.
4. При повреждении данных восстановить исходный backup вместо попытки ручного удаления колонок.
5. Вернуть предыдущий образ приложения и проверить health/read-only smoke.

Baseline — marker-ревизия для уже существующей схемы 0.17.8. Его нельзя ставить
на неизвестную или пустую production-БД без предварительной сверки схемы.
