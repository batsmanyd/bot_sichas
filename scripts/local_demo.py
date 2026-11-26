"""Loopback demonstration launcher, shipped only in the demo image."""
import base64
import io
import os
import re
import secrets
from collections import defaultdict, deque
from datetime import timedelta
from threading import Lock
from pathlib import Path

data_directory = Path(os.getenv("SICHAS_DEMO_DATA_DIR", "/demo-data")).resolve()
database_url = f"sqlite:///{data_directory / 'sichas-demo.db'}"
if (os.getenv("SICHAS_LOCAL_DEMO") != "1"
        or os.getenv("APP_ENV") != "development"
        or os.getenv("PUBLIC_URL") != "http://127.0.0.1:8081"
        or os.getenv("DATABASE_URL") != database_url
        or any(os.getenv(name) for name in (
            "RAILWAY_ENVIRONMENT", "SECRET_KEY", "SELFIE_ENCRYPTION_KEY",
            "TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_CLIENT_SECRET",
            "TELEGRAM_CLIENT_ID", "ALLOW_TEST_AUTH",
        ))):
    raise SystemExit("Use compose.demo.yaml with its isolated loopback settings.")

ROLES = {
    "anna": ("900000001", "Анна · участник"),
    "maxim": ("900000002", "Максим · участник"),
    "customer": ("900000003", "Вера · заказчик"),
    "worker": ("900000004", "Олег · исполнитель"),
    "organizer": ("900000005", "Ирина · организатор"),
    "coordinator": ("900000006", "Лев · координатор"),
    "moderator": ("900000007", "Майя · модератор поручений"),
    "admin": ("900000008", "Денис · администратор"),
    "new": ("900000009", "Новый участник"),
}
os.environ["PRIVATE_TASK_TELEGRAM_IDS"] = ",".join(ROLES[role][0] for role in ("customer", "worker"))
os.environ["TASK_MODERATOR_TELEGRAM_IDS"] = ROLES["moderator"][0]
os.environ["ADMIN_TELEGRAM_IDS"] = ROLES["admin"][0]

for name in ("SECRET_KEY", "SELFIE_ENCRYPTION_KEY"):
    path = data_directory / name
    try:
        with path.open("x") as key:
            key.write(secrets.token_urlsafe(32))
        path.chmod(0o600)
    except FileExistsError:
        pass
    os.environ[name] = path.read_text().strip()

from flask import abort, redirect, render_template_string, request, session
from PIL import Image
import main

app = main.app
app.config["SESSION_COOKIE_NAME"] = "sichas_demo_session"
db = main.db
real_utcnow = main.utcnow
clock_offset = timedelta()
clock_lock = Lock()
deliveries = defaultdict(lambda: deque(maxlen=20))


def demo_telegram(method, payload):
    if method == "sendMessage" and str(payload["chat_id"]) in {value[0] for value in ROLES.values()}:
        deliveries[str(payload["chat_id"])].appendleft(payload["text"])
    return {"ok": True}


main.utcnow = lambda: real_utcnow() + clock_offset
main.telegram_api = demo_telegram
main.BOT_TOKEN = "local-demo"

with app.app_context():
    picture = io.BytesIO()
    Image.new("RGB", (300, 300), "#7abd8a").save(picture, "JPEG")
    image = main.encrypt_selfie("data:image/jpeg;base64," + base64.b64encode(picture.getvalue()).decode())
    for role, (telegram_id, name) in ROLES.items():
        if main.User.query.filter_by(telegram_id=telegram_id).first():
            continue
        user = main.User(telegram_id=telegram_id, name=name)
        db.session.add(user)
        db.session.flush()
        if role != "new":
            db.session.add(main.UserProfile(user_id=user.id, age=30, gender="female", city="Минск"))
            db.session.add(main.ProfileSelfie(user_id=user.id, image=image, about="Участник локального демо"))
    organizer = main.User.query.filter_by(telegram_id=ROLES["organizer"][0]).one()
    coordinator = main.User.query.filter_by(telegram_id=ROLES["coordinator"][0]).one()
    if not db.session.get(main.VerifiedOrganizer, organizer.id):
        db.session.add(main.VerifiedOrganizer(user_id=organizer.id, name="Городская команда", active=True))
        db.session.flush()
        db.session.add(main.OrganizerCoordinator(organizer_id=organizer.id, user_id=coordinator.id,
                                                 label="Координатор команды"))
    db.session.commit()


@app.route("/demo", methods=["GET", "POST"])
def demo_switch():
    global clock_offset
    if request.method == "POST":
        if not secrets.compare_digest(session.get("demo_nonce", ""), request.form.get("nonce", "")) or not session.get("demo_nonce"):
            abort(403)
        if request.form.get("advance") == "hour":
            if not main.current_user():
                abort(403)
            with clock_lock:
                clock_offset += timedelta(hours=1, minutes=1)
                main.process_presence_reminders()
                main.process_company_watches()
                main.expire_tasks()
                main.process_user_telegram_outbox()
            return redirect("/demo")
        if request.form.get("deliver") == "now":
            if not main.current_user():
                abort(403)
            with clock_lock:
                main.process_company_watches()
                main.process_user_telegram_outbox()
            return redirect("/demo")
        role = request.form.get("role")
        if role not in ROLES and role != "guest":
            abort(400)
        user = main.User.query.filter_by(telegram_id=ROLES[role][0]).first() if role in ROLES else None
        session.clear()
        if user:
            session["user_id"] = user.id
        return redirect("/")
    nonce = session.setdefault("demo_nonce", secrets.token_urlsafe(24))
    user = main.current_user()
    return render_template_string("""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Сейчас · локальное демо</title>
<style>body{margin:2rem auto;padding:0 1rem;max-width:36rem;background:#17251b;color:white;font:18px system-ui}
form{display:grid;gap:.65rem}button,a{font:inherit;color:#17251b;background:#c9f3ce;padding:.7rem;border-radius:.6rem}
a{display:inline-block;margin-top:1rem}</style>
<h1>Локальное демо</h1><p>Выберите роль. Данные общие для ролей и хранятся в отдельной локальной базе.</p>
<form method="post"><input type="hidden" name="nonce" value="{{nonce}}">
{% for role, info in roles.items() %}<button name="role" value="{{role}}">{{info[1]}}</button>{% endfor %}
<button name="role" value="guest">Гость</button></form><a href="/">Вернуться в приложение</a>
{% if user %}<h2>Уведомления {{user.name}}</h2>
<form method="post"><input type="hidden" name="nonce" value="{{nonce}}">
<button name="deliver" value="now">Обработать фоновые уведомления</button>
<button name="advance" value="hour">Продвинуть время на 1 час и 1 минуту</button></form>
<ul>{% for message in messages %}<li>{{message}}</li>{% endfor %}</ul>{% endif %}""",
                                  nonce=nonce, roles=ROLES, user=user,
                                  messages=list(deliveries[user.telegram_id]) if user else [])


@app.after_request
def demo_navigation(response):
    if request.path in {"/", "/admin"} and response.status_code == 200 and response.mimetype == "text/html":
        response.direct_passthrough = False
        content = response.get_data(as_text=True)
        banner = ('<div style="padding:8px;text-align:center;background:#17251b">'
                  '<a style="color:white" href="/demo">Локальное демо · сменить роль</a></div>')
        response.set_data(re.sub(r"(<body\b[^>]*>)", lambda match: match[0] + banner, content, count=1))
        response.headers.pop("ETag", None)
        response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    published_by_docker = data_directory == Path("/demo-data")
    app.run(host="0.0.0.0" if published_by_docker else "127.0.0.1",
            port=8080 if published_by_docker else 8081)
