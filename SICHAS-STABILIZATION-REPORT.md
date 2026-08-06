# Отчёт о стабилизации «Сейчас»

Дата: 2026-08-06  
Исходный commit: `ca8bcd9a4998f8a264a3a447624e215f1ccb129a`  
Рабочая ветка: `fix/sichas-stabilization-admin`

## 1. Коммиты

1. `8831b7d` — `fix(security): protect files and require production secrets`
2. `a0d2ebd` — `feat: add durable sync and moderation backend`
3. `dfba3df` — `feat(ui): add resilient updates and moderation dashboard`
4. `c698709` — `test: cover offline drafts and complaint privacy`

Документация и скриншоты фиксируются отдельным завершающим commit после формирования этого отчёта.

`main` не изменялась. Merge, push, pull request, deploy, обращения к Railway, production-БД и реальному Telegram не выполнялись.

## 2. Изменённые файлы

- `main.py`, `index.html`, `admin.html`, `README.md`, `requirements.txt`;
- `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`;
- шесть файлов в `migrations/versions/`;
- `tests/test_mvp.py`, `tests/test_postgres_concurrency.py`;
- `docs/MIGRATIONS.md`, `docs/screenshots/*.png`;
- `SICHAS-TECH-AUDIT.md`, `SICHAS-STABILIZATION-REPORT.md`.

## 3. Архитектурные решения

### Безопасность и конфигурация

- Корневой catch-all заменён allowlist публичных файлов. Приватные Python-файлы, `.git`, тесты, БД, служебные и конфигурационные файлы не выдаются.
- Production запускается только при наличии `SECRET_KEY`, `SELFIE_ENCRYPTION_KEY`, `DATABASE_URL`, `PUBLIC_URL`, `TELEGRAM_BOT_TOKEN` и `ADMIN_TELEGRAM_IDS`.
- Ключ сессии отделён от ключа шифрования selfie. Формат ciphertext содержит версию ключа; старый формат читается для безопасной миграции.
- Добавлены лимит тела запроса, CSP и другие security headers.
- Каждый admin endpoint проверяет Telegram ID на сервере. Изменяющие операции защищены CSRF-токеном и проверкой Origin.

### Целостность встреч и повторные запросы

- Принятие interest выполняется одной транзакцией. Для PostgreSQL используются advisory lock и `SELECT FOR UPDATE`; после захвата блокировки повторно проверяются статус, вместимость и активные встречи.
- Один пользователь не может одновременно быть владельцем или участником второй активной подтверждённой встречи.
- Конфликты возвращают HTTP 409 с машинно-читаемыми кодами.
- `OperationRecord` обеспечивает replay результата mutation-запросов по `X-Operation-ID`; frontend генерирует operation ID и блокирует повторные действия.

### Синхронизация

- `SyncRevision` хранит DB-backed ревизии `feed`, `user`, `notifications`, `room` и `admin`. Ревизия изменяется в той же транзакции, что и предметное состояние.
- `/api/sync-state` отдаёт только лёгкое состояние и проверяет доступ к room.
- Polling: 2 секунды в открытой room, 5 секунд на основном экране, 15 секунд в скрытой вкладке; при ошибках — exponential backoff до 30 секунд.
- Полные данные перечитываются только при изменении ревизии. При возврате вкладки выполняется немедленная синхронизация.
- SSE сохранён только под `ENABLE_SSE`; production default — `false`. Ревизии переживают restart и видны всем workers через БД.

### Жалобы, админка и Telegram

- Жалобы имеют постоянное состояние, модератора, решение, причину и историю действий; физическое удаление не используется.
- Три разных reporter срабатывают ровно на третьей жалобе. Повторный reporter не увеличивает порог. Автоскрытие временное и не равно окончательной блокировке.
- Спор о неявке создаёт отдельный moderation case. Оспаривание временно исключает штраф до решения; отклонение спора возвращает последствия.
- `/admin` содержит метрики, фильтры, карточку, ограниченный просмотр доказательств и полный набор запрошенных действий. Просмотр доказательств — отдельный POST, ограничен сообщениями встречи в окне ±2 часа и записывается в audit trail.
- Идентификаторы пользователей в админке — HMAC-псевдонимы. Координаты, телефон, selfie, initData, токены и полный чат не возвращаются.
- `NotificationOutbox` создаётся в транзакции жалобы. Worker сохраняет статус доставки и использует retry/backoff. Администраторам отправляются только минимальные уведомления о жалобах; room-chat и обычные bot-сообщения outbox не создают.
- `/start` отправляет одно сообщение с кнопкой. Свободный текст получает один rate-limited нейтральный ответ и не пересылается администратору.

### Клиент и данные

- Уведомления корректно учитывают `read_at`.
- Room возвращает последние 100 сообщений в хронологическом порядке.
- Текст сообщения очищается только после HTTP ack; при ошибке draft остаётся, кнопка временно блокируется, статус называется «Сохранено на сервере», а не «Доставлено».
- Возвращён browser zoom. Групповой интерфейс показывает всех участников и адресует благодарность/жалобу/неявку конкретному человеку.

## 4. Миграции

1. `0001_baseline_existing_schema` — маркер схемы 0.17.8 без DDL.
2. `0002_sync_revisions` — устойчивые ревизии синхронизации.
3. `0003_moderation_state` — состояние жалоб, moderator fields и audit trail.
4. `0004_notification_outbox` — transactional outbox.
5. `0005_operation_idempotency` — operation IDs и сохранённые ответы.
6. `0006_integrity_indexes` — индексы активных встреч, interest и жалоб.

Проверено локально:

- upgrade копии старой SQLite-схемы до `0006_integrity_indexes (head)`;
- генерация полного offline SQL для PostgreSQL;
- downgrade-функции присутствуют, но destructive downgrade не выполнялся.

Инструкция backup/upgrade/rollback: `docs/MIGRATIONS.md`.

## 5. Результаты тестов

Команда: `python -m unittest discover -s tests -v`.

- Всего обнаружено: **45**.
- Успешно: **42**.
- Ошибки/падения: **0**.
- Пропущено: **3** PostgreSQL concurrency tests.
- Время финального полного прогона: **489.920 s**.
- Дополнительный прогон усиленных проверок draft/PII/outbox: **2/2 OK**, 21.316 s.
- `python -m compileall -q main.py tests migrations`: успешно.
- В репозитории нет `package.json`; npm build/lint/typecheck неприменимы.
- Браузерный smoke-test `/admin` на локальной SQLite-базе: доступ/рендеринг/мобильная карточка успешны, console errors/warnings: 0.

Проверены regression-сценарии: приватные файлы и traversal; production fail-fast; Telegram initData; две активные встречи; idempotency; revisions; права и CSRF админки; outbox и отсутствие PII; третья независимая жалоба; bot free text; отсутствие room-chat в outbox; read notifications; последние 100 сообщений; draft; прежние пользовательские сценарии.

## 6. PostgreSQL concurrency tests

Создан `tests/test_postgres_concurrency.py` со сценариями:

- два владельца одновременно принимают одного пользователя;
- два участника одновременно занимают последнее место группы;
- повтор операции возвращает сохранённый результат.

Результат: **не выполнены на реальном PostgreSQL**. На Windows-хосте отсутствуют Docker, `psql` и локальная служба PostgreSQL; `TEST_POSTGRES_URL` намеренно не задан, поэтому 3 теста корректно skipped. PostgreSQL dialect и offline SQL Alembic проверены, но это не доказывает поведение блокировок под реальной конкуренцией.

## 7. Скриншоты админки

- `docs/screenshots/admin-dashboard-mobile.png`
- `docs/screenshots/admin-report-detail-mobile.png`

Снимки сделаны локально с синтетической жалобой. Реальные модерационные действия не выполнялись.

## 8. Новые/обязательные Railway variables

- `APP_ENV=production`
- `SECRET_KEY=<отдельный сильный секрет сессий>`
- `SELFIE_ENCRYPTION_KEY=<отдельный сильный ключ>`
- `SELFIE_ENCRYPTION_KEY_VERSION=v1`
- `DATABASE_URL=<staging PostgreSQL URL>`
- `PUBLIC_URL=<staging HTTPS URL без завершающего slash>`
- `TELEGRAM_BOT_TOKEN=<только отдельный staging bot token>`
- `ADMIN_TELEGRAM_IDS=<числовые Telegram ID через запятую>`
- `ENABLE_SSE=false`
- `ALLOW_TEST_AUTH=false` перед доступом тестировщиков.

Необязательные `TELEGRAM_CLIENT_ID` и `TELEGRAM_CLIENT_SECRET` нужны только для browser OIDC. Значения секретов не записаны в код или отчёт.

## 9. Пошаговый staging deploy plan

1. Создать отдельные staging Railway project, PostgreSQL и Telegram bot; не копировать production credentials или персональные данные.
2. Снять backup staging DB и проверить восстановление в отдельную базу.
3. Настроить перечисленные variables, оставить `ENABLE_SSE=false`; временный `ALLOW_TEST_AUTH=true` допустим только в закрытом техническом контуре и должен быть выключен до пользовательского теста.
4. Проверить `alembic current`. Для существующей схемы 0.17.8 после сверки структуры выполнить `alembic stamp 0001_baseline`, затем `alembic upgrade head`. Для чистой БД применить согласованный bootstrap согласно `docs/MIGRATIONS.md`.
5. Сохранить `alembic upgrade head --sql` как change artifact и проверить DDL DBA/вторым разработчиком.
6. Запустить приложение одним staging worker, проверить `/api/version`, `/api/session`, 404 приватных файлов и отсутствие startup secret values в логах.
7. Запустить `TEST_POSTGRES_URL` на отдельной disposable DB и добиться 3/3 concurrency tests без skipped.
8. Выполнить сценарий двух пользователей: параллельный accept, последняя позиция группы, потерянный HTTP-ответ/retry, restart приложения и multi-worker polling. Зафиксировать p95 видимости изменения ≤3 s.
9. Проверить тестовую жалобу: одна запись БД, один outbox delivery, одно Telegram-сообщение только staging-админу, deep link в нужную карточку, audit trail evidence/action.
10. Проверить, что `/start`, свободный текст и room-chat не создают admin outbox и не пересылаются.
11. Провести мобильный smoke-test Android/iOS WebView, плохую сеть, background/foreground и reconnect.
12. Только после зелёных пунктов 7–11 принять отдельное решение о production deploy. Этот план ничего не развёртывает автоматически.

## 10. Rollback plan

1. Остановить входящий staging-трафик/worker и зафиксировать время инцидента.
2. Не выполнять downgrade поверх новых записей без backup: миграции 0003–0005 удаляют данные при downgrade.
3. Предпочтительный rollback — вернуть предыдущий application image/commit и восстановить pre-migration snapshot в отдельную PostgreSQL-базу, затем переключить staging connection после проверки.
4. Если схема уже мигрирована и старый код совместим с добавочными таблицами/колонками, безопаснее оставить схему на head и откатить только приложение после smoke-test.
5. Отключить staging webhook/outbox worker при повторной доставке; не удалять outbox или жалобы вручную.
6. Проверить `alembic current`, целостность meeting/interest/report/outbox и только затем вернуть трафик.

## 11. Оставшиеся риски

- Нет фактического прогона PostgreSQL блокировок и индексов под конкурентной нагрузкой.
- Не измерена реальная p95 задержка синхронизации на staging с несколькими Gunicorn workers и мобильной сетью; проектное значение polling — 2 s, но локальный smoke-test не заменяет измерение.
- Telegram API имеет необратимое окно crash-after-send-before-status-update: outbox минимизирует дубли и повторно использует одну запись, но без provider idempotency абсолютное exactly-once недостижимо.
- Baseline предполагает точное соответствие существующей staging-схемы версии 0.17.8; перед `stamp` нужна ручная сверка.
- Есть SQLAlchemy legacy warning в тесте (`Query.get`) и ResourceWarning тестового file response; функциональные тесты зелёные, но предупреждения стоит убрать до расширения CI.
- Нет нагрузочного теста polling/outbox и полноценной матрицы реальных Telegram WebView/устройств.

## 12. Заключение

**NOT READY** для deployment/production и формального `READY FOR STAGING` по заданным критериям.

Точная причина: три обязательных PostgreSQL concurrency tests не были выполнены на реальном изолированном PostgreSQL, а SLA синхронизации ≤2–3 секунд не измерен на staging с несколькими workers. Код, миграции, локальный suite и защищённая админка подготовлены для следующего этапа — изолированного staging validation.
