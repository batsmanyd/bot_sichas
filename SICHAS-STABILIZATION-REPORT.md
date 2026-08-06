# Отчёт о стабилизации «Сейчас»

Дата: 2026-08-06  
Исходный commit: `ca8bcd9a4998f8a264a3a447624e215f1ccb129a`  
Рабочая ветка: `fix/sichas-stabilization-admin`

## 1. Коммиты

1. `8831b7d` — `fix(security): protect files and require production secrets`
2. `a0d2ebd` — `feat: add durable sync and moderation backend`
3. `dfba3df` — `feat(ui): add resilient updates and moderation dashboard`
4. `c698709` — `test: cover offline drafts and complaint privacy`
5. `b56c5da` — `docs: add stabilization evidence and runbooks`
6. `78346f3` — `fix: close remaining stabilization guards`
7. `7fac043` — `test: cover durable room sync and sanitized logs`
8. `d3ab2e9` — `fix(security): require PostgreSQL in production`
9. `6b9efeb` — `test: close response resources and remove legacy API`

Финальное обновление отчёта фиксируется отдельным документационным commit.

`main` не изменялась. Merge, push, pull request, deploy, обращения к Railway, production-БД и реальному Telegram не выполнялись.

## 2. Изменённые файлы

- `main.py`, `index.html`, `admin.html`, `README.md`, `requirements.txt`;
- `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`;
- семь файлов в `migrations/versions/`;
- `tests/test_mvp.py`, `tests/test_postgres_concurrency.py`;
- `docs/MIGRATIONS.md`, `docs/screenshots/*.png`;
- `SICHAS-TECH-AUDIT.md`, `SICHAS-STABILIZATION-REPORT.md`.

## 3. Архитектурные решения

### Матрица относительно аудита 2026-08-05

| Область | Статус | Результат / остаток |
|---|---|---|
| Публичная выдача приватных файлов | Исправлено полностью | Allowlist; 404 для `main.py`, `.git`, `tests`, `*.db` и traversal |
| Production secrets | Исправлено полностью | Fail-fast, отдельный selfie key, HTTPS, числовые admin IDs, запрет test auth и SQLite в production |
| Целостность встреч | Исправлено по коду и unit-тестам | PostgreSQL locks, одна активная встреча, capacity и idempotency; реальный PostgreSQL прогон ожидается |
| Синхронизация | Исправлено архитектурно | DB revisions и polling 2/5/15 секунд; staging p95 ещё не измерен |
| SSE capacity | Исправлено полностью для production default | SSE выключен, остаётся только feature flag |
| Админка и audit log | Исправлено полностью по локальным тестам | Права, CSRF/Origin, blocked/deep-link, история, минимизация PII |
| Telegram жалоб | Исправлено по unit-тестам | Per-admin outbox, dedupe, retry/backoff; реальный staging bot ещё не проверен |
| Порог жалоб | Исправлено полностью | `COUNT(DISTINCT reporter_id)`, срабатывание ровно на третьей |
| Notifications/chat/draft/права | Исправлено полностью | `read_at`, latest 100, draft до ack, guards двойного нажатия, owner/group rights |
| Alembic | Исправлено полностью по локальной миграционной проверке | Production не вызывает `create_all` и требует head `0007_runtime_guards` |
| Structured logs | Исправлено для production | JSON, request ID, latency; тела, query string, координаты и user ID не логируются |
| PostgreSQL concurrency execution | Не выполнено | Нет доступного изолированного PostgreSQL на текущем Windows-хосте |
| Durable reminders, N+1/geo, device-token revocation, CI/E2E | Не выполнено | Остаточные P1/P2, не блокируют локальную логику админки, но требуют следующего цикла |

### Безопасность и конфигурация

- Корневой catch-all заменён allowlist публичных файлов. Приватные Python-файлы, `.git`, тесты, БД, служебные и конфигурационные файлы не выдаются.
- Production запускается только при наличии `SECRET_KEY`, `SELFIE_ENCRYPTION_KEY`, `DATABASE_URL`, `PUBLIC_URL`, `TELEGRAM_BOT_TOKEN` и `ADMIN_TELEGRAM_IDS`.
- Production принимает только PostgreSQL, абсолютный HTTPS `PUBLIC_URL`, положительные числовые admin IDs и `ALLOW_TEST_AUTH=false`.
- Production не вызывает `create_all`: запуск разрешён только на ожидаемой Alembic revision.
- Ключ сессии отделён от ключа шифрования selfie. Формат ciphertext содержит версию ключа; старый формат читается для безопасной миграции.
- Добавлены лимит тела запроса, CSP и другие security headers.
- Каждый admin endpoint проверяет Telegram ID на сервере. Изменяющие операции защищены CSRF-токеном и проверкой Origin.
- Production logs имеют JSON-формат и correlation/request ID без request body, query string, Telegram ID и точных координат.

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
- `NotificationOutbox` создаёт отдельную deduplicated запись для каждого администратора в транзакции жалобы. Worker сохраняет статус доставки и использует retry/backoff. Room-chat и обычные bot-сообщения outbox не создают.
- Telegram deep link `/admin?report=ID` открывает точную карточку. Добавлены blocked-фильтр и счётчик.
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
7. `0007_runtime_guards` — индексы blocked/moderation-фильтров и ожидаемый production schema head.

Проверено локально:

- схема создана непосредственным выполнением `main.py` из исходного Git commit `ca8bcd9a`, затем успешно выполнены `stamp 0001_baseline` и upgrade до `0007_runtime_guards (head)`;
- проверен цикл `downgrade 0007 → 0006 → upgrade 0007`;
- генерация полного offline SQL для PostgreSQL;
- production runtime schema guard ожидает строго `0007_runtime_guards`.

Инструкция backup/upgrade/rollback: `docs/MIGRATIONS.md`.

## 5. Результаты тестов

Команда: `python -m unittest discover -s tests -v`.

- Всего обнаружено: **50**.
- Успешно: **46**.
- Ошибки/падения: **0**.
- Пропущено: **4** PostgreSQL concurrency tests.
- Время финального полного прогона: **555.463 s**.
- Дополнительный строгий прогон трёх file-response/admin тестов с `ResourceWarning` как ошибкой: **3/3 OK**, 33.504 s.
- `python -m compileall -q main.py tests migrations`: успешно.
- В репозитории нет `package.json`; npm build/lint/typecheck неприменимы.
- Браузерный smoke-test `/admin` на локальной SQLite-базе: доступ/рендеринг/мобильная карточка успешны, console errors/warnings: 0.

Проверены regression-сценарии: приватные файлы и traversal; production fail-fast/HTTPS/PostgreSQL/test-auth guard; Telegram initData; две активные встречи; idempotency; все room revisions; права, CSRF и blocked/deep-link админки; outbox и отсутствие PII; третья независимая жалоба и повтор reporter; bot free text; отсутствие room-chat в outbox; read notifications; последние 100 сообщений; draft; прежние пользовательские сценарии.

## 6. PostgreSQL concurrency tests

Создан `tests/test_postgres_concurrency.py` со сценариями:

- два владельца одновременно принимают одного пользователя;
- два участника одновременно занимают последнее место группы;
- повтор операции возвращает сохранённый результат.
- два одновременных accept с одним operation ID дают один результат и один replay.

Результат: **не выполнены на реальном PostgreSQL**. На Windows-хосте отсутствуют Docker, `psql` и локальная служба PostgreSQL; поэтому 4 теста корректно skipped. Destructive suite запускается только при точном `DATABASE_URL=TEST_POSTGRES_URL` и `ALLOW_DESTRUCTIVE_TEST_DB=true`, что исключает случайное удаление другой БД. PostgreSQL dialect и offline SQL Alembic проверены, но это не доказывает поведение блокировок под реальной конкуренцией.

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
3. Настроить перечисленные variables, оставить `ENABLE_SSE=false` и `ALLOW_TEST_AUTH=false`; production-mode guard не допускает тестовый вход даже на staging.
4. Проверить `alembic current`. Для существующей схемы 0.17.8 после сверки структуры выполнить `alembic stamp 0001_baseline`, затем `alembic upgrade head`. Для чистой БД применить согласованный bootstrap согласно `docs/MIGRATIONS.md`.
5. Сохранить `alembic upgrade head --sql` как change artifact и проверить DDL DBA/вторым разработчиком.
6. Запустить приложение одним staging worker, проверить `/api/version`, `/api/session`, 404 приватных файлов и отсутствие startup secret values в логах.
7. На отдельной disposable DB задать одинаковые `DATABASE_URL` и `TEST_POSTGRES_URL`, затем одноразово `ALLOW_DESTRUCTIVE_TEST_DB=true` и добиться 4/4 concurrency tests без skipped. Никогда не направлять эти variables на staging/production DB.
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
- Device token действует до 180 дней и обычный logout его не отзывает; для отзыва всех устройств нужна versioned token/session model.
- Напоминания остаются process-local scheduler, а не отдельным durable job/outbox; при нескольких workers требуется staging-проверка дублей.
- Feed/trust/interests сохраняют N+1/full-scan участки, нет PostGIS и нагрузочного профиля polling/outbox.
- Нет dependency lock с hashes и полноценного CI lint/E2E/accessibility контура.
- Нет нагрузочного теста polling/outbox и полноценной матрицы реальных Telegram WebView/устройств.

## 12. Заключение

**NOT READY** для deployment/production и формального `READY FOR STAGING` по заданным критериям.

Точная причина: четыре обязательных PostgreSQL concurrency tests не были выполнены на реальном изолированном PostgreSQL, а SLA синхронизации ≤2–3 секунд не измерен на staging с несколькими workers. Код, миграции, локальный suite и защищённая админка подготовлены для следующего этапа — изолированного staging validation.
