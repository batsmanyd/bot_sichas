import base64
import hashlib
import hmac
import json
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qsl, urlencode

import jwt
import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, literal, select,
)
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://web-production-4d1a9.up.railway.app").rstrip("/")
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
CLIENT_ID = os.getenv("TELEGRAM_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TELEGRAM_CLIENT_SECRET", "").strip()
CALLBACK_URL = f"{PUBLIC_URL}/auth/telegram/callback"
ALLOW_TEST_AUTH = os.getenv("ALLOW_TEST_AUTH", "false").lower() == "true"
ADMIN_TELEGRAM_IDS = {value.strip() for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if value.strip()}

database_url = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'sichas.db')}")
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]

secret_seed = os.getenv("SECRET_KEY") or CLIENT_SECRET or BOT_TOKEN
if not secret_seed:
    secret_seed = secrets.token_hex(32)

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = hashlib.sha256(f"{secret_seed}|seichas-session".encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_SECURE=PUBLIC_URL.startswith("https://") and not app.testing,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


engine_options = {"pool_pre_ping": True}
if database_url.startswith("sqlite:"):
    engine_options["connect_args"] = {"check_same_thread": False}
engine = create_engine(database_url, **engine_options)
SessionLocal = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))


class QueryProperty:
    def __get__(self, obj, owner):
        return SessionLocal.query(owner)


class ModelBase:
    query = QueryProperty()


Model = declarative_base(cls=ModelBase)


class Database:
    Model = Model
    Column = Column
    Integer = Integer
    String = String
    Float = Float
    Text = Text
    DateTime = DateTime
    ForeignKey = ForeignKey
    relationship = staticmethod(relationship)
    session = SessionLocal
    select = staticmethod(select)
    literal = staticmethod(literal)

    @staticmethod
    def create_all():
        Model.metadata.create_all(engine)

    @staticmethod
    def get_or_404(model, identity):
        value = SessionLocal.get(model, identity)
        if value is None:
            from flask import abort
            abort(404)
        return value


db = Database()


@app.teardown_appcontext
def close_database_session(_exception=None):
    SessionLocal.remove()


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    username = db.Column(db.String(80))
    phone_number = db.Column(db.String(40))
    picture = db.Column(db.Text)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Presence(db.Model):
    __tablename__ = "presence"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    active_until = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    user = db.relationship("User")


class Meeting(db.Model):
    __tablename__ = "meeting"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False)
    description = db.Column(db.String(180), nullable=False)
    format = db.Column(db.String(20), nullable=False, default="one")
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    owner = db.relationship("User")


class Interest(db.Model):
    __tablename__ = "interest"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_interest_meeting_user"),)


class MeetingPlace(db.Model):
    __tablename__ = "meeting_place"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    confirmed = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class PlaceVote(db.Model):
    __tablename__ = "place_vote"
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey("meeting_place.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    __table_args__ = (UniqueConstraint("place_id", "user_id", name="uq_place_vote_user"),)


class ChatMessage(db.Model):
    __tablename__ = "chat_message"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    text = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class MeetingState(db.Model):
    __tablename__ = "meeting_state"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="active")
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class MeetingEvent(db.Model):
    __tablename__ = "meeting_event"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    kind = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(180))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class MeetingFeedback(db.Model):
    __tablename__ = "meeting_feedback"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    trace = db.Column(db.String(180), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_feedback_meeting_user"),)


class UserReport(db.Model):
    __tablename__ = "user_report"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    target_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    reason = db.Column(db.String(180), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class UserBlock(db.Model):
    __tablename__ = "user_block"
    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_user_block"),)


class ActionLog(db.Model):
    __tablename__ = "action_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    action = db.Column(db.String(30), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class UserModeration(db.Model):
    __tablename__ = "user_moderation"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    hidden_until = db.Column(db.DateTime(timezone=True), nullable=False)
    reason = db.Column(db.String(180), nullable=False)


VALID_CATEGORIES = {"cafe", "walk", "talk", "active", "shop", "help", "leisure"}
CATEGORY_ICONS = {
    "cafe": "☕", "walk": "🚶", "talk": "💬", "active": "🚲",
    "shop": "🛍", "help": "🤝", "leisure": "🎬",
}


def normalize_dt(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify(error="Нужна регистрация через Telegram"), 401
        return view(*args, **kwargs)
    return wrapped


def json_body():
    return request.get_json(silent=True) or {}


def consume_action(user_id, action, limit, window_seconds):
    since = utcnow() - timedelta(seconds=window_seconds)
    count = ActionLog.query.filter(ActionLog.user_id == user_id, ActionLog.action == action,
                                   ActionLog.created_at >= since).count()
    if count >= limit:
        return False
    db.session.add(ActionLog(user_id=user_id, action=action))
    return True


def user_hidden(user_id):
    moderation = UserModeration.query.filter_by(user_id=user_id).first()
    return bool(moderation and normalize_dt(moderation.hidden_until) > utcnow())


def valid_coordinates(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        return -90 <= lat <= 90 and -180 <= lon <= 180
    except (TypeError, ValueError):
        return False


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def safe_point(lat, lon, identifier):
    digest = hashlib.sha256(f"{app.secret_key}|{identifier}".encode()).digest()
    lat_offset = ((digest[0] / 255) - 0.5) * 0.005
    lon_offset = ((digest[1] / 255) - 0.5) * 0.008
    if abs(lat_offset) < 0.0005:
        lat_offset = 0.0005 if lat_offset >= 0 else -0.0005
    if abs(lon_offset) < 0.0008:
        lon_offset = 0.0008 if lon_offset >= 0 else -0.0008
    return round(lat + lat_offset, 4), round(lon + lon_offset, 4)


def notify_user(user_id, text):
    if not BOT_TOKEN:
        return
    user = db.session.get(User, user_id)
    if not user or not user.telegram_id or user.telegram_id.startswith("test-"):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": user.telegram_id, "text": text}, timeout=5).raise_for_status()
    except requests.RequestException:
        app.logger.warning("Telegram notification failed for user %s", user_id)


def upsert_telegram_user(data):
    telegram_id = str(data.get("id") or "").strip()
    if not telegram_id:
        raise ValueError("Telegram не передал идентификатор пользователя")
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, name="Участник")
        db.session.add(user)
    full_name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")])).strip()
    user.name = (data.get("name") or full_name or user.name or "Участник")[:80]
    user.username = (data.get("username") or "")[:80] or None
    user.phone_number = (data.get("phone_number") or "")[:40] or user.phone_number
    user.picture = data.get("photo_url") or data.get("picture") or user.picture
    db.session.commit()
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    return user


def verify_mini_app_init_data(init_data):
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash or not BOT_TOKEN:
        return None
    auth_date = int(pairs.get("auth_date", "0") or 0)
    if abs(int(utcnow().timestamp()) - auth_date) > 86400:
        return None
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None


def oidc_configured():
    return bool(CLIENT_ID and CLIENT_SECRET)


def base64url_sha256(value):
    digest = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return response


@app.get("/health")
def health():
    db.session.execute(db.select(db.literal(1)))
    return jsonify(ok=True)


@app.get("/api/session")
def api_session():
    user = current_user()
    return jsonify(
        authenticated=bool(user),
        telegram_configured=bool(BOT_TOKEN or oidc_configured()),
        mini_app_configured=bool(BOT_TOKEN),
        test_auth_enabled=ALLOW_TEST_AUTH,
        is_admin=bool(user and user.telegram_id in ADMIN_TELEGRAM_IDS),
        user={
            "id": user.id,
            "name": user.name,
            "username": user.username,
            "picture": user.picture,
        } if user else None,
    )


@app.post("/auth/telegram-mini-app")
def telegram_mini_app():
    user_data = verify_mini_app_init_data(json_body().get("init_data", ""))
    if not user_data:
        return jsonify(error="Не удалось подтвердить запуск из Telegram"), 401
    user = upsert_telegram_user(user_data)
    return jsonify(ok=True, user={"id": user.id, "name": user.name, "username": user.username})


@app.post("/auth/test")
def test_auth():
    if not ALLOW_TEST_AUTH:
        return jsonify(error="Тестовый вход отключён"), 404
    suffix = str(json_body().get("user", "1"))[:20]
    user = upsert_telegram_user({"id": f"test-{suffix}", "first_name": f"Тест {suffix}"})
    return jsonify(ok=True, user={"id": user.id, "name": user.name})


@app.get("/auth/telegram/start")
def telegram_start():
    if not oidc_configured():
        return redirect("/?auth=not_configured")
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    session.update(oidc_state=state, oidc_nonce=nonce, oidc_verifier=verifier)
    params = {
        "client_id": CLIENT_ID, "redirect_uri": CALLBACK_URL, "response_type": "code",
        "scope": "openid profile phone", "state": state, "nonce": nonce,
        "code_challenge": base64url_sha256(verifier), "code_challenge_method": "S256",
    }
    return redirect(f"https://oauth.telegram.org/auth?{urlencode(params)}")


@app.get("/auth/telegram/callback")
def telegram_callback():
    if request.args.get("error") or request.args.get("state") != session.get("oidc_state"):
        return redirect("/?auth=failed")
    try:
        token_response = requests.post(
            "https://oauth.telegram.org/token",
            data={"grant_type": "authorization_code", "code": request.args.get("code", ""),
                  "redirect_uri": CALLBACK_URL, "client_id": CLIENT_ID,
                  "code_verifier": session.get("oidc_verifier", "")},
            auth=(CLIENT_ID, CLIENT_SECRET), timeout=15,
        )
        token_response.raise_for_status()
        id_token = token_response.json()["id_token"]
        signing_key = jwt.PyJWKClient("https://oauth.telegram.org/.well-known/jwks.json").get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, signing_key.key, algorithms=["RS256"], audience=CLIENT_ID,
                            issuer="https://oauth.telegram.org")
        if session.get("oidc_nonce") and claims.get("nonce") != session.get("oidc_nonce"):
            raise ValueError("Invalid nonce")
        upsert_telegram_user(claims)
        return redirect("/?auth=success")
    except Exception:
        app.logger.exception("Telegram authentication failed")
        session.clear()
        return redirect("/?auth=failed")


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/feed")
def feed():
    user = current_user()
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = min(max(request.args.get("radius", 3, type=float), 1), 12)
    category = request.args.get("category", "")
    now = utcnow()
    result = []
    blocked_ids = set()
    if user:
        blocked_ids = {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=user.id).all()}
        blocked_ids |= {b.blocker_id for b in UserBlock.query.filter_by(blocked_id=user.id).all()}

    if valid_coordinates(lat, lon):
        presences = Presence.query.filter(Presence.active_until > now).all()
        for presence in presences:
            if user and presence.user_id == user.id:
                continue
            if presence.user_id in blocked_ids:
                continue
            if user_hidden(presence.user_id):
                continue
            if category in VALID_CATEGORIES and presence.category != category:
                continue
            distance = haversine_km(lat, lon, presence.latitude, presence.longitude)
            if distance > radius:
                continue
            point = safe_point(presence.latitude, presence.longitude, f"p{presence.id}")
            result.append({
                "kind": "person", "id": presence.id, "icon": CATEGORY_ICONS[presence.category],
                "name": presence.user.name if user else "Участник рядом", "category": presence.category,
                "description": "Открыт к общению", "distance_km": round(distance, 1),
                "latitude": point[0], "longitude": point[1], "expires_at": normalize_dt(presence.active_until).isoformat(),
            })

        meetings = Meeting.query.filter(Meeting.expires_at > now).all()
        interested_ids = set()
        if user:
            interested_ids = {i.meeting_id for i in Interest.query.filter_by(user_id=user.id).all()}
        for meeting in meetings:
            if meeting.owner_id in blocked_ids:
                continue
            if user_hidden(meeting.owner_id):
                continue
            if category in VALID_CATEGORIES and meeting.category != category:
                continue
            distance = haversine_km(lat, lon, meeting.latitude, meeting.longitude)
            if distance > radius:
                continue
            point = safe_point(meeting.latitude, meeting.longitude, f"m{meeting.id}")
            result.append({
                "kind": "meeting", "id": meeting.id, "icon": CATEGORY_ICONS[meeting.category],
                "name": meeting.owner.name if user else "Открытая встреча", "category": meeting.category,
                "description": meeting.description, "format": meeting.format, "distance_km": round(distance, 1),
                "latitude": point[0], "longitude": point[1], "mine": bool(user and meeting.owner_id == user.id),
                "interested": meeting.id in interested_ids, "expires_at": normalize_dt(meeting.expires_at).isoformat(),
            })
    result.sort(key=lambda item: item["distance_km"])
    return jsonify(items=result)


@app.post("/api/location")
@login_required
def location():
    user = current_user()
    data = json_body()
    if not valid_coordinates(data.get("latitude"), data.get("longitude")):
        return jsonify(error="Некорректная геолокация"), 400
    user.latitude, user.longitude = float(data["latitude"]), float(data["longitude"])
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/presence")
@login_required
def set_presence():
    user = current_user()
    data = json_body()
    category = data.get("category")
    if category not in VALID_CATEGORIES:
        return jsonify(error="Выберите занятие"), 400
    lat = data.get("latitude", user.latitude)
    lon = data.get("longitude", user.longitude)
    if not valid_coordinates(lat, lon):
        return jsonify(error="Разрешите геолокацию, чтобы стать видимым рядом"), 400
    presence = Presence.query.filter_by(user_id=user.id).first() or Presence(user_id=user.id)
    presence.category, presence.latitude, presence.longitude = category, float(lat), float(lon)
    presence.active_until = utcnow() + timedelta(minutes=60)
    user.latitude, user.longitude = float(lat), float(lon)
    db.session.add(presence)
    db.session.commit()
    return jsonify(ok=True, active_until=normalize_dt(presence.active_until).isoformat())


@app.delete("/api/presence")
@login_required
def stop_presence():
    user = current_user()
    Presence.query.filter_by(user_id=user.id).delete()
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/presence")
@login_required
def get_presence():
    presence = Presence.query.filter_by(user_id=current_user().id).first()
    active = bool(presence and normalize_dt(presence.active_until) > utcnow())
    return jsonify(active=active, category=presence.category if active else None,
                   active_until=normalize_dt(presence.active_until).isoformat() if active else None)


@app.post("/api/meetings")
@login_required
def create_meeting():
    user = current_user()
    data = json_body()
    category = data.get("category")
    description = str(data.get("description", "")).strip()[:180]
    meeting_format = data.get("format", "one")
    lat, lon = data.get("latitude", user.latitude), data.get("longitude", user.longitude)
    if category not in VALID_CATEGORIES or not description:
        return jsonify(error="Выберите занятие и цель встречи"), 400
    if meeting_format not in {"one", "group"}:
        return jsonify(error="Некорректный формат встречи"), 400
    if not valid_coordinates(lat, lon):
        return jsonify(error="Разрешите геолокацию для создания встречи"), 400
    if not consume_action(user.id, "meeting", 5, 3600):
        return jsonify(error="Слишком много встреч за час. Попробуйте позже"), 429
    meeting = Meeting(
        owner_id=user.id, category=category, description=description, format=meeting_format,
        latitude=float(lat), longitude=float(lon), expires_at=utcnow() + timedelta(minutes=60),
    )
    user.latitude, user.longitude = float(lat), float(lon)
    db.session.add(meeting)
    db.session.commit()
    return jsonify(ok=True, id=meeting.id), 201


@app.post("/api/meetings/<int:meeting_id>/interest")
@login_required
def express_interest(meeting_id):
    user = current_user()
    meeting = db.get_or_404(Meeting, meeting_id)
    if normalize_dt(meeting.expires_at) <= utcnow():
        return jsonify(error="Эта встреча уже завершена"), 409
    if not consume_action(user.id, "interest", 12, 3600):
        return jsonify(error="Слишком много откликов за час. Попробуйте позже"), 429
    if meeting.owner_id == user.id:
        return jsonify(error="Это ваша встреча"), 400
    if (UserBlock.query.filter_by(blocker_id=user.id, blocked_id=meeting.owner_id).first() or
            UserBlock.query.filter_by(blocker_id=meeting.owner_id, blocked_id=user.id).first()):
        return jsonify(error="Отклик на эту встречу недоступен"), 403
    accepted_count = Interest.query.filter_by(meeting_id=meeting.id, status="accepted").count()
    if meeting.format == "one" and accepted_count:
        return jsonify(error="У этой встречи уже есть подтверждённый участник"), 409
    if meeting.format == "group" and accepted_count >= 6:
        return jsonify(error="Группа уже набрана"), 409
    interest = Interest.query.filter_by(meeting_id=meeting.id, user_id=user.id).first()
    if not interest:
        db.session.add(Interest(meeting_id=meeting.id, user_id=user.id))
        db.session.commit()
        notify_user(meeting.owner_id, f"Новый отклик на встречу «{meeting.description}» от {user.name}")
    return jsonify(ok=True)


def interest_payload(interest, viewer):
    meeting = db.session.get(Meeting, interest.meeting_id)
    participant = db.session.get(User, interest.user_id)
    accepted = interest.status == "accepted"
    return {
        "id": interest.id,
        "meeting_id": meeting.id,
        "description": meeting.description,
        "category": meeting.category,
        "format": meeting.format,
        "status": interest.status,
        "participant": {
            "name": participant.name,
            # Personal details are revealed only after the meeting owner accepts.
            "picture": participant.picture if accepted else None,
            "username": participant.username if accepted else None,
        },
        "owner": {
            "name": meeting.owner.name,
            "picture": meeting.owner.picture if accepted else None,
            "username": meeting.owner.username if accepted else None,
        },
        "can_decide": viewer.id == meeting.owner_id and interest.status == "pending",
    }


@app.get("/api/interests")
@login_required
def list_interests():
    user = current_user()
    owned_meeting_ids = [row.id for row in Meeting.query.filter_by(owner_id=user.id).all()]
    incoming = (Interest.query.filter(Interest.meeting_id.in_(owned_meeting_ids)).all()
                if owned_meeting_ids else [])
    outgoing = Interest.query.filter_by(user_id=user.id).all()
    return jsonify(
        incoming=[interest_payload(item, user) for item in incoming],
        outgoing=[interest_payload(item, user) for item in outgoing],
    )


@app.post("/api/interests/<int:interest_id>/decision")
@login_required
def decide_interest(interest_id):
    user = current_user()
    interest = db.get_or_404(Interest, interest_id)
    meeting = db.session.get(Meeting, interest.meeting_id)
    if meeting.owner_id != user.id:
        return jsonify(error="Решение принимает создатель встречи"), 403
    if interest.status != "pending":
        return jsonify(error="По этому отклику решение уже принято"), 409
    decision = json_body().get("decision")
    if decision not in {"accepted", "rejected"}:
        return jsonify(error="Выберите принять или отклонить"), 400
    interest.status = decision
    if decision == "accepted" and meeting.format == "one":
        (Interest.query.filter_by(meeting_id=meeting.id, status="pending")
         .filter(Interest.id != interest.id).update({"status": "rejected"}, synchronize_session=False))
    if decision == "accepted" and meeting.format == "group":
        accepted_count = Interest.query.filter_by(meeting_id=meeting.id, status="accepted").count()
        if accepted_count > 6:
            db.session.rollback()
            return jsonify(error="В группе может быть не больше 6 участников"), 409
    db.session.commit()
    result_text = "принят" if decision == "accepted" else "отклонён"
    notify_user(interest.user_id, f"Ваш отклик на встречу «{meeting.description}» {result_text}")
    return jsonify(ok=True, interest=interest_payload(interest, user))


def meeting_member(meeting, user):
    if meeting.owner_id == user.id:
        return Interest.query.filter_by(meeting_id=meeting.id, status="accepted").first() is not None
    return Interest.query.filter_by(meeting_id=meeting.id, user_id=user.id, status="accepted").first() is not None


def meeting_room_or_error(meeting_id):
    meeting = db.get_or_404(Meeting, meeting_id)
    if not meeting_member(meeting, current_user()):
        return None, (jsonify(error="Чат откроется после подтверждения участия"), 403)
    return meeting, None


def room_payload(meeting, user):
    places = MeetingPlace.query.filter_by(meeting_id=meeting.id).order_by(MeetingPlace.id).all()
    votes = PlaceVote.query.filter(PlaceVote.place_id.in_([p.id for p in places])).all() if places else []
    vote_counts, my_votes = {}, set()
    for vote in votes:
        vote_counts[vote.place_id] = vote_counts.get(vote.place_id, 0) + 1
        if vote.user_id == user.id:
            my_votes.add(vote.place_id)
    messages = ChatMessage.query.filter_by(meeting_id=meeting.id).order_by(ChatMessage.id).limit(100).all()
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    events = MeetingEvent.query.filter_by(meeting_id=meeting.id).order_by(MeetingEvent.id).all()
    feedback = MeetingFeedback.query.filter_by(meeting_id=meeting.id).order_by(MeetingFeedback.id).all()
    member_ids = accepted_user_ids(meeting)
    return {"meeting": {"id": meeting.id, "description": meeting.description, "format": meeting.format,
                         "is_owner": meeting.owner_id == user.id, "status": state.status if state else "active"},
            "places": [{"id": p.id, "title": p.title, "votes": vote_counts.get(p.id, 0),
                        "voted": p.id in my_votes, "confirmed": bool(p.confirmed)} for p in places],
            "messages": [{"id": m.id, "name": db.session.get(User, m.user_id).name, "text": m.text,
                          "mine": m.user_id == user.id, "created_at": normalize_dt(m.created_at).isoformat()}
                         for m in messages],
            "events": [{"kind": e.kind, "name": db.session.get(User, e.user_id).name,
                        "note": e.note or ""} for e in events],
            "traces": [{"name": db.session.get(User, f.user_id).name, "text": f.trace} for f in feedback],
            "participants": [{"id": uid, "name": db.session.get(User, uid).name, "mine": uid == user.id}
                             for uid in member_ids]}


@app.get("/api/meetings/<int:meeting_id>/room")
@login_required
def get_meeting_room(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    return error or jsonify(room_payload(meeting, current_user()))


@app.post("/api/meetings/<int:meeting_id>/places")
@login_required
def propose_place(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    title = str(json_body().get("title", "")).strip()[:120]
    if len(title) < 2:
        return jsonify(error="Укажите место встречи"), 400
    db.session.add(MeetingPlace(meeting_id=meeting.id, user_id=current_user().id, title=title))
    db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, current_user())), 201


@app.post("/api/places/<int:place_id>/vote")
@login_required
def vote_place(place_id):
    place = db.get_or_404(MeetingPlace, place_id)
    meeting, error = meeting_room_or_error(place.meeting_id)
    if error:
        return error
    vote = PlaceVote.query.filter_by(place_id=place.id, user_id=current_user().id).first()
    db.session.delete(vote) if vote else db.session.add(PlaceVote(place_id=place.id, user_id=current_user().id))
    db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, current_user()))


@app.post("/api/places/<int:place_id>/confirm")
@login_required
def confirm_place(place_id):
    place = db.get_or_404(MeetingPlace, place_id)
    meeting = db.session.get(Meeting, place.meeting_id)
    if meeting.owner_id != current_user().id:
        return jsonify(error="Место подтверждает создатель встречи"), 403
    MeetingPlace.query.filter_by(meeting_id=meeting.id).update({"confirmed": 0})
    place.confirmed = 1
    db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, current_user()))


@app.post("/api/meetings/<int:meeting_id>/messages")
@login_required
def send_message(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    if not consume_action(current_user().id, "message", 12, 60):
        return jsonify(error="Слишком много сообщений. Подождите минуту"), 429
    text = str(json_body().get("text", "")).strip()[:500]
    if not text:
        return jsonify(error="Напишите сообщение"), 400
    db.session.add(ChatMessage(meeting_id=meeting.id, user_id=current_user().id, text=text))
    db.session.commit()
    for member_id in accepted_user_ids(meeting) - {current_user().id}:
        notify_user(member_id, f"{current_user().name}: {text[:120]}")
    return jsonify(ok=True, room=room_payload(meeting, current_user())), 201


def state_for(meeting):
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if not state:
        state = MeetingState(meeting_id=meeting.id, status="active")
        db.session.add(state)
    return state


def accepted_user_ids(meeting):
    return {meeting.owner_id} | {i.user_id for i in Interest.query.filter_by(
        meeting_id=meeting.id, status="accepted").all()}


@app.post("/api/meetings/<int:meeting_id>/lifecycle")
@login_required
def meeting_lifecycle(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    user, data = current_user(), json_body()
    action = data.get("action")
    if action not in {"late", "cancel", "complete", "no_show"}:
        return jsonify(error="Неизвестное действие"), 400
    state = state_for(meeting)
    if state.status != "active":
        return jsonify(error="Встреча уже завершена или отменена"), 409
    target_id = data.get("target_user_id")
    if action in {"cancel", "complete"} and meeting.owner_id != user.id:
        return jsonify(error="Это действие доступно создателю встречи"), 403
    if action == "no_show":
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return jsonify(error="Укажите участника, который не пришёл"), 400
        if target_id == user.id or target_id not in accepted_user_ids(meeting):
            return jsonify(error="Некорректный участник"), 400
    if action == "cancel":
        state.status = "cancelled"
    elif action == "complete":
        state.status = "completed"
    db.session.add(MeetingEvent(meeting_id=meeting.id, user_id=user.id, target_user_id=target_id,
                                kind=action, note=str(data.get("note", "")).strip()[:180] or None))
    db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, user))


@app.post("/api/meetings/<int:meeting_id>/feedback")
@login_required
def leave_feedback(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if not state or state.status != "completed":
        return jsonify(error="След можно оставить после завершения встречи"), 409
    trace = str(json_body().get("trace", "")).strip()[:180]
    if len(trace) < 2:
        return jsonify(error="Напишите короткое впечатление"), 400
    feedback = MeetingFeedback.query.filter_by(meeting_id=meeting.id, user_id=current_user().id).first()
    if not feedback:
        feedback = MeetingFeedback(meeting_id=meeting.id, user_id=current_user().id, trace=trace)
        db.session.add(feedback)
    else:
        feedback.trace = trace
    db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, current_user()))


@app.post("/api/meetings/<int:meeting_id>/report")
@login_required
def report_user(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    data, reporter = json_body(), current_user()
    if not consume_action(reporter.id, "report", 5, 86400):
        return jsonify(error="Лимит жалоб на сегодня исчерпан"), 429
    try:
        target_id = int(data.get("target_user_id"))
    except (TypeError, ValueError):
        return jsonify(error="Укажите участника"), 400
    if target_id == reporter.id or target_id not in accepted_user_ids(meeting):
        return jsonify(error="Некорректный участник"), 400
    reason = str(data.get("reason", "")).strip()[:180]
    if len(reason) < 3:
        return jsonify(error="Опишите причину жалобы"), 400
    if UserReport.query.filter_by(meeting_id=meeting.id, reporter_id=reporter.id, target_id=target_id).first():
        return jsonify(error="Вы уже отправили жалобу на этого участника"), 409
    db.session.add(UserReport(meeting_id=meeting.id, reporter_id=reporter.id,
                              target_id=target_id, reason=reason))
    if data.get("block") and not UserBlock.query.filter_by(blocker_id=reporter.id, blocked_id=target_id).first():
        db.session.add(UserBlock(blocker_id=reporter.id, blocked_id=target_id))
    recent = utcnow() - timedelta(hours=24)
    report_count = UserReport.query.filter(UserReport.target_id == target_id,
                                           UserReport.created_at >= recent).count()
    if report_count >= 3:
        moderation = UserModeration.query.filter_by(user_id=target_id).first()
        if not moderation:
            moderation = UserModeration(user_id=target_id, hidden_until=utcnow() + timedelta(hours=24),
                                        reason="Три независимые жалобы за 24 часа")
            db.session.add(moderation)
        else:
            moderation.hidden_until = utcnow() + timedelta(hours=24)
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/admin/reports")
@login_required
def admin_reports():
    user = current_user()
    if user.telegram_id not in ADMIN_TELEGRAM_IDS:
        return jsonify(error="Нет доступа"), 403
    reports = UserReport.query.order_by(UserReport.id.desc()).limit(200).all()
    return jsonify(items=[{"id": item.id, "meeting_id": item.meeting_id,
                           "reporter": db.session.get(User, item.reporter_id).name,
                           "target_id": item.target_id,
                           "target": db.session.get(User, item.target_id).name,
                           "reason": item.reason,
                           "created_at": normalize_dt(item.created_at).isoformat()} for item in reports])


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    full_path = os.path.join(BASE_DIR, path)
    if os.path.isfile(full_path):
        return send_from_directory(BASE_DIR, path)
    return send_from_directory(BASE_DIR, "index.html")


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
