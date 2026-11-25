# Сейчас — MVP для общения рядом

Это не дейтинг. Пользователь включает статус до ручного выключения, выбирает занятие, видит активных людей и встречи рядом и может отправить отклик.

## Что работает

- безопасный вход из Telegram Mini App с серверной проверкой `initData`;
- PostgreSQL на Railway и SQLite для локальной разработки;
- реальные пользователи, геолокация, постоянный статус с напоминанием через час;
- встречи и повторно-безопасные отклики;
- уведомления о встречах в приложении, жалобы через Telegram только администраторам;
- доверие по завершённым встречам, благодарностям и неявкам;
- полноценные точки мест встречи с названием и координатами;
- возвращаемые приглашения и отметка «Развивает клуб»;
- радиус 3/5/10/12 км;
- приблизительные зоны на карте и диапазоны расстояния;
- гостевой просмотр демо и публичных акций без персональных карточек.

## Переменные Railway

- `DATABASE_URL` — добавляется сервисом PostgreSQL;
- `SECRET_KEY` — случайная длинная строка;
- `SELFIE_ENCRYPTION_KEY` — отдельная случайная длинная строка для шифрования селфи;
- `SELFIE_ENCRYPTION_KEY_VERSION` — версия текущего ключа, например `v1`;
- `PUBLIC_URL` — публичный адрес Railway без завершающего `/`;
- `TELEGRAM_BOT_TOKEN` — токен `@vmeste_rjadom_bot` (старое имя `BOT_TOKEN` тоже поддерживается);
- `ADMIN_TELEGRAM_IDS` — Telegram ID администраторов через запятую, без имён пользователей;
- `ENABLE_SSE=false` — SSE не используется как основной production-механизм синхронизации;
- `ALLOW_TEST_AUTH=false`.

В Railway также необходимо задать `APP_ENV=production`. В production приложение
завершает запуск, если отсутствует обязательная конфигурация, используется не
PostgreSQL/HTTPS, включён тестовый вход или схема БД не находится на Alembic
revision `0008_safety_good_deeds`. Временный production-ключ не генерируется.

`TELEGRAM_CLIENT_ID` и `TELEGRAM_CLIENT_SECRET` нужны только для необязательного входа из обычного браузера. Основной вход Mini App использует `TELEGRAM_BOT_TOKEN`.

## Проверка

```bash
python -m unittest discover -s tests -v
```

Destructive PostgreSQL concurrency tests разрешены только для отдельной
одноразовой БД, когда `DATABASE_URL` точно совпадает с `TEST_POSTGRES_URL` и
явно задано `ALLOW_DESTRUCTIVE_TEST_DB=true`.

Railway запускает приложение командой из `Procfile`.

## Windows и Docker Desktop

В Docker Desktop включите Linux containers (WSL 2). В PowerShell из корня
репозитория создайте `.env` с двумя независимыми постоянными ключами:

```powershell
function New-SichasKey {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    [Convert]::ToBase64String($bytes)
}
if (-not (Test-Path .env)) {
    @("SECRET_KEY=$(New-SichasKey)", "SELFIE_ENCRYPTION_KEY=$(New-SichasKey)") | Set-Content .env -Encoding ascii
}
docker compose up --build -d --wait
Invoke-RestMethod http://127.0.0.1:8080/health
```

Откройте [http://127.0.0.1:8080](http://127.0.0.1:8080). Кнопка «Демо» запускает
сценарий с вымышленными участниками. Карта использует внешние тайлы. Скачивание
образа и зависимостей при первой сборке требует доступа в интернет.

`docker compose down` останавливает локальное приложение. SQLite хранится в томе
`app-data`. Сохраните исходный `.env` вместе с резервной копией БД. Для переноса
данных старой версии сначала выполните [миграцию](docs/MIGRATIONS.md) на копии.
