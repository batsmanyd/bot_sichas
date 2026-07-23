import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qsl, quote, urlencode

import jwt
import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, literal, select,
)
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://upbeat-reverence-production-c0b7.up.railway.app").rstrip("/")
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
CLIENT_ID = os.getenv("TELEGRAM_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TELEGRAM_CLIENT_SECRET", "").strip()
CALLBACK_URL = f"{PUBLIC_URL}/auth/telegram/callback"
ALLOW_TEST_AUTH = os.getenv("ALLOW_TEST_AUTH", "false").lower() == "true"
ADMIN_TELEGRAM_IDS = {value.strip() for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if value.strip()}
BOT_USERNAME = os.getenv("BOT_USERNAME", "vmeste_rjadom_bot").strip().lstrip("@")

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
selfie_cipher = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(f"{app.secret_key}|selfie-storage-v1".encode()).digest()
))
app.config.update(
    SESSION_COOKIE_SECURE=PUBLIC_URL.startswith("https://") and not app.testing,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
device_token_serializer = URLSafeTimedSerializer(app.secret_key, salt="sichas-device-auth-v1")
DEVICE_TOKEN_MAX_AGE = 180 * 24 * 60 * 60


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


def encrypt_selfie(value):
    return "enc:" + selfie_cipher.encrypt(value.encode()).decode()


def decrypt_selfie(value):
    if not value:
        return None
    if not value.startswith("enc:"):
        return value  # Existing records are migrated when the profile is next saved.
    try:
        return selfie_cipher.decrypt(value[4:].encode()).decode()
    except InvalidToken:
        app.logger.error("Stored selfie could not be decrypted")
        return None


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


class UserProfile(db.Model):
    __tablename__ = "user_profile"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    age = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    city = db.Column(db.String(80), nullable=False, default="Минск")
    terms_accepted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ProfileSelfie(db.Model):
    __tablename__ = "profile_selfie"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    image = db.Column(db.Text, nullable=False)
    visibility = db.Column(db.String(20), nullable=False, default="mutual")
    about = db.Column(db.String(160), nullable=False)
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


class PhotoConsent(db.Model):
    __tablename__ = "photo_consent"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_photo_consent_meeting_user"),)


class MeetingPlace(db.Model):
    __tablename__ = "meeting_place"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    confirmed = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class MeetingPlaceLocation(db.Model):
    __tablename__ = "meeting_place_location"
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey("meeting_place.id"), unique=True, nullable=False, index=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(20), nullable=False, default="map")


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


class MeetingThanks(db.Model):
    __tablename__ = "meeting_thanks"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    giver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("meeting_id", "giver_id", "receiver_id", name="uq_meeting_thanks"),)


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


class TrafficVisit(db.Model):
    __tablename__ = "traffic_visit"
    id = db.Column(db.Integer, primary_key=True)
    visitor_hash = db.Column(db.String(64), nullable=False, index=True)
    source = db.Column(db.String(60), nullable=False, default="direct", index=True)
    medium = db.Column(db.String(40), nullable=False, default="none")
    campaign = db.Column(db.String(80), nullable=False, default="public_beta", index=True)
    landing_path = db.Column(db.String(120), nullable=False, default="/")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class UserModeration(db.Model):
    __tablename__ = "user_moderation"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    hidden_until = db.Column(db.DateTime(timezone=True), nullable=False)
    reason = db.Column(db.String(180), nullable=False)


class InviteAccount(db.Model):
    __tablename__ = "invite_account"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    available = db.Column(db.Integer, nullable=False, default=3)


class Invitation(db.Model):
    __tablename__ = "invitation"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(40), unique=True, nullable=False, index=True)
    inviter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    claimed_by = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    status = db.Column(db.String(20), nullable=False, default="created")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    claimed_at = db.Column(db.DateTime(timezone=True))


class UserNotification(db.Model):
    __tablename__ = "user_notification"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False, default="info")
    text = db.Column(db.String(300), nullable=False)
    dedupe_key = db.Column(db.String(120), unique=True, index=True)
    read_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class GeocodeCache(db.Model):
    __tablename__ = "geocode_cache"
    id = db.Column(db.Integer, primary_key=True)
    coordinate_key = db.Column(db.String(40), unique=True, nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class AuthHandoff(db.Model):
    __tablename__ = "auth_handoff"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))
    used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


VALID_CATEGORIES = {"cafe", "walk", "talk", "active", "shop", "help", "leisure"}
CATEGORY_ICONS = {
    "cafe": "☕", "walk": "🚶", "talk": "💬", "active": "🚲",
    "shop": "🛍", "help": "🤝", "leisure": "🎬",
}
CATEGORY_MEETING_TITLES = {
    "cafe": "Выпить кофе или чай", "walk": "Прогуляться вместе",
    "talk": "Поговорить", "active": "Заняться активностью",
    "shop": "Сходить за покупками", "help": "Помочь друг другу",
    "leisure": "Провести досуг вместе",
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


def clean_tracking_value(value, fallback, max_length):
    value = str(value or "").strip().lower()
    cleaned = "".join(char for char in value if char.isalnum() or char in "._-")
    return (cleaned or fallback)[:max_length]


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


def invite_account(user_id):
    account = InviteAccount.query.filter_by(user_id=user_id).first()
    if not account:
        account = InviteAccount(user_id=user_id, available=3)
        db.session.add(account)
    return account


def claim_invitation(user, token):
    token = str(token or "").removeprefix("invite_")[:40]
    invitation = Invitation.query.filter_by(token=token, status="created").first()
    if not invitation or invitation.inviter_id == user.id:
        return False
    if Invitation.query.filter_by(claimed_by=user.id).first():
        return False
    if Meeting.query.filter_by(owner_id=user.id).first() or Interest.query.filter_by(user_id=user.id).first():
        return False
    invitation.claimed_by = user.id
    invitation.status = "claimed"
    invitation.claimed_at = utcnow()
    db.session.commit()
    return True


def reward_completed_invites(member_ids):
    invitations = Invitation.query.filter(Invitation.claimed_by.in_(member_ids),
                                           Invitation.status == "claimed").all()
    for invitation in invitations:
        account = invite_account(invitation.inviter_id)
        account.available += 1
        invitation.status = "rewarded"


def trust_payload(user_id):
    completed_states = MeetingState.query.filter_by(status="completed").all()
    completed_ids = {state.meeting_id for state in completed_states}
    completed = 0
    for meeting_id in completed_ids:
        meeting = db.session.get(Meeting, meeting_id)
        if not meeting:
            continue
        if meeting.owner_id == user_id or Interest.query.filter_by(
                meeting_id=meeting_id, user_id=user_id, status="accepted").first():
            completed += 1
    thanks = MeetingThanks.query.filter_by(receiver_id=user_id).count()
    no_shows = MeetingEvent.query.filter_by(target_user_id=user_id, kind="no_show").count()
    score = max(20, min(100, 70 + min(completed * 4, 20) + min(thanks * 3, 15) - min(no_shows * 15, 45)))
    if completed == 0 and thanks == 0 and no_shows == 0:
        level = "Новый участник"
    elif score >= 90:
        level = "Высокое доверие"
    elif score >= 70:
        level = "Надёжный участник"
    else:
        level = "Требует внимания"
    develops_club = Invitation.query.filter_by(inviter_id=user_id, status="rewarded").count() > 0
    return {
        "score": score,
        "level": level,
        "completed_meetings": completed,
        "thanks": thanks,
        "no_shows": no_shows,
        "develops_club": develops_club,
    }


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


def notify_user(user_id, text, kind="info", dedupe_key=None):
    if dedupe_key and UserNotification.query.filter_by(dedupe_key=dedupe_key).first():
        return False
    db.session.add(UserNotification(
        user_id=user_id, kind=kind, text=str(text)[:300], dedupe_key=dedupe_key,
    ))
    db.session.commit()
    if not BOT_TOKEN:
        return True
    user = db.session.get(User, user_id)
    if not user or not user.telegram_id or user.telegram_id.startswith("test-"):
        return True
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": user.telegram_id, "text": text}, timeout=5).raise_for_status()
    except requests.RequestException:
        app.logger.warning("Telegram notification failed for user %s", user_id)
    return True


def process_presence_reminders():
    threshold = utcnow() - timedelta(hours=1)
    active = Presence.query.filter(Presence.active_until > utcnow(), Presence.updated_at <= threshold).all()
    for presence in active:
        started = normalize_dt(presence.updated_at).replace(microsecond=0).isoformat()
        notify_user(
            presence.user_id,
            "Вы всё ещё открыты для общения. Оставить статус включённым или выключить его в приложении?",
            kind="presence_reminder",
            dedupe_key=f"presence-reminder:{presence.id}:{started}",
        )


def presence_reminder_loop():
    while True:
        time.sleep(60)
        try:
            with app.app_context():
                process_presence_reminders()
        except Exception:
            app.logger.exception("Presence reminder worker failed")


TELEGRAM_WEBHOOK_SECRET = hashlib.sha256(f"{app.secret_key}|telegram-webhook".encode()).hexdigest()[:48]
telegram_bot_configured = False


def telegram_api(method, payload):
    if not BOT_TOKEN:
        return None
    response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=5)
    response.raise_for_status()
    return response.json()


def configure_telegram_bot():
    global telegram_bot_configured
    if telegram_bot_configured or not BOT_TOKEN or app.testing:
        return
    telegram_bot_configured = True
    try:
        telegram_api("setWebhook", {
            "url": f"{PUBLIC_URL}/telegram/webhook",
            "secret_token": TELEGRAM_WEBHOOK_SECRET,
            "allowed_updates": ["message"],
        })
        telegram_api("setMyCommands", {"commands": [
            {"command": "start", "description": "Открыть приложение «Сейчас»"},
        ]})
        telegram_api("setChatMenuButton", {"menu_button": {
            "type": "web_app", "text": "Открыть «Сейчас»", "web_app": {"url": PUBLIC_URL},
        }})
    except requests.RequestException:
        telegram_bot_configured = False
        app.logger.exception("Telegram bot setup failed")


@app.before_request
def ensure_telegram_bot_configured():
    configure_telegram_bot()


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
    # The app does not request or retain a phone number; Telegram is used only to confirm identity.
    user.phone_number = None
    user.picture = data.get("photo_url") or data.get("picture") or user.picture
    db.session.commit()
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    return user


def issue_device_token(user):
    return device_token_serializer.dumps({"user_id": user.id, "telegram_id": user.telegram_id})


def restore_device_session(token):
    try:
        payload = device_token_serializer.loads(token, max_age=DEVICE_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.session.get(User, payload.get("user_id"))
    if not user or user.telegram_id != str(payload.get("telegram_id") or ""):
        return None
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    return user


def claim_auth_handoff(user, start_param):
    if not start_param.startswith("login_"):
        return False
    token = start_param.removeprefix("login_")[:64]
    handoff = AuthHandoff.query.filter_by(token=token).first()
    if not handoff or handoff.used_at or normalize_dt(handoff.expires_at) <= utcnow():
        return False
    handoff.user_id = user.id
    handoff.completed_at = utcnow()
    db.session.commit()
    return True


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
    return jsonify(ok=True, telegram_bot_configured=telegram_bot_configured)


@app.get("/api/session")
def api_session():
    user = current_user()
    profile = UserProfile.query.filter_by(user_id=user.id).first() if user else None
    selfie = ProfileSelfie.query.filter_by(user_id=user.id).first() if user else None
    return jsonify(
        authenticated=bool(user),
        telegram_configured=bool(BOT_TOKEN or oidc_configured()),
        mini_app_configured=bool(BOT_TOKEN),
        oidc_configured=oidc_configured(),
        test_auth_enabled=ALLOW_TEST_AUTH,
        is_admin=bool(user and user.telegram_id in ADMIN_TELEGRAM_IDS),
        profile_completed=bool(profile and selfie),
        user={
            "id": user.id,
            "name": user.name,
            "username": user.username,
            "picture": user.picture,
            "trust": trust_payload(user.id),
        } if user else None,
    )


@app.post("/api/traffic")
def track_traffic_visit():
    data = json_body()
    visitor_id = str(data.get("visitor_id") or "").strip()
    if not 8 <= len(visitor_id) <= 100:
        return jsonify(error="Некорректный идентификатор визита"), 400
    source = clean_tracking_value(data.get("source"), "direct", 60)
    medium = clean_tracking_value(data.get("medium"), "none", 40)
    campaign = clean_tracking_value(data.get("campaign"), "public_beta", 80)
    landing_path = str(data.get("landing_path") or "/").strip()[:120]
    if not landing_path.startswith("/"):
        landing_path = "/"
    visitor_hash = hashlib.sha256(
        f"{app.secret_key}|traffic-v1|{visitor_id}".encode()
    ).hexdigest()
    since = utcnow() - timedelta(hours=24)
    existing = TrafficVisit.query.filter(
        TrafficVisit.visitor_hash == visitor_hash,
        TrafficVisit.source == source,
        TrafficVisit.campaign == campaign,
        TrafficVisit.created_at >= since,
    ).first()
    if not existing:
        db.session.add(TrafficVisit(
            visitor_hash=visitor_hash,
            source=source,
            medium=medium,
            campaign=campaign,
            landing_path=landing_path,
        ))
        db.session.commit()
    return jsonify(ok=True, counted=not bool(existing))


def user_profile_payload(user):
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    selfie = ProfileSelfie.query.filter_by(user_id=user.id).first()
    return {
        "completed": bool(profile and selfie), "name": user.name,
        "age": profile.age if profile else None,
        "gender": profile.gender if profile else None,
        "city": profile.city if profile else "Минск",
        "about": selfie.about if selfie else "",
        "selfie_present": bool(selfie),
        "selfie_preview": decrypt_selfie(selfie.image) if selfie else None,
        "selfie_visibility": selfie.visibility if selfie else "mutual",
        "trust": trust_payload(user.id),
    }


@app.get("/api/profile")
@login_required
def get_profile():
    return jsonify(profile=user_profile_payload(current_user()))


@app.post("/api/profile")
@login_required
def save_profile():
    user, data = current_user(), json_body()
    name = str(data.get("name", "")).strip()[:40]
    try:
        age = int(data.get("age"))
    except (TypeError, ValueError):
        return jsonify(error="Укажите возраст"), 400
    gender = str(data.get("gender", ""))
    about = str(data.get("about", "")).strip()[:160]
    selfie_image = str(data.get("selfie", ""))
    selfie_visibility = str(data.get("selfie_visibility", "mutual"))
    if len(name) < 2:
        return jsonify(error="Укажите имя — не короче двух букв"), 400
    if not 18 <= age <= 100:
        return jsonify(error="Приложение доступно пользователям от 18 до 100 лет"), 400
    if gender not in {"male", "female"}:
        return jsonify(error="Выберите вариант в поле «Пол»"), 400
    if len(about) < 20:
        return jsonify(error="Напишите одну короткую фразу о себе — минимум 20 символов"), 400
    if selfie_visibility not in {"mutual", "accepted", "hidden"}:
        return jsonify(error="Выберите, кому можно показывать селфи"), 400
    if data.get("terms_accepted") is not True:
        return jsonify(error="Подтвердите правила безопасных встреч"), 400
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    selfie = ProfileSelfie.query.filter_by(user_id=user.id).first()
    if selfie_image:
        if not selfie_image.startswith("data:image/jpeg;base64,") or len(selfie_image) > 700_000:
            return jsonify(error="Не удалось обработать селфи. Сделайте новое фото"), 400
    elif not selfie:
        return jsonify(error="Добавьте свежее селфи"), 400
    if not profile:
        profile = UserProfile(user_id=user.id, age=age, gender=gender, city="Минск")
        db.session.add(profile)
    else:
        profile.age, profile.gender = age, gender
        profile.terms_accepted_at = utcnow()
    if not selfie:
        selfie = ProfileSelfie(user_id=user.id, image=encrypt_selfie(selfie_image),
                               visibility=selfie_visibility, about=about)
        db.session.add(selfie)
    else:
        if selfie_image:
            selfie.image = encrypt_selfie(selfie_image)
        selfie.visibility, selfie.about = selfie_visibility, about
    user.name = name
    db.session.commit()
    return jsonify(ok=True, profile=user_profile_payload(user))


@app.post("/telegram/webhook")
def telegram_webhook():
    if not BOT_TOKEN or not secrets.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TELEGRAM_WEBHOOK_SECRET):
        return jsonify(error="Недоступно"), 403
    message = json_body().get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id:
        message_text = str(message.get("text") or "").strip()
        code_match = re.fullmatch(r"(?:/start\s+login_)?(\d{8})", message_text)
        if code_match:
            handoff = AuthHandoff.query.filter_by(token=code_match.group(1)).first()
            if handoff and not handoff.used_at and normalize_dt(handoff.expires_at) > utcnow():
                sender = message.get("from") or {}
                try:
                    user = upsert_telegram_user(sender)
                    handoff.user_id = user.id
                    handoff.completed_at = utcnow()
                    db.session.commit()
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Вход подтверждён. Вернитесь в установленное приложение «Сейчас».",
                    })
                except (ValueError, requests.RequestException):
                    app.logger.exception("Telegram code login failed")
                return jsonify(ok=True)
            try:
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Код не найден или его время истекло. Получите новый код в приложении «Сейчас».",
                })
            except requests.RequestException:
                app.logger.exception("Telegram expired code reply failed")
            return jsonify(ok=True)
        start_payload = ""
        if message_text.startswith("/start "):
            start_payload = message_text.split(maxsplit=1)[1][:80]
        web_app_url = PUBLIC_URL
        if start_payload.startswith(("login_", "invite_")):
            web_app_url = f"{PUBLIC_URL}/?handoff={quote(start_payload, safe='')}"
        try:
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": "Бот теперь используется только для безопасного входа и уведомлений приложения «Сейчас».",
                "reply_markup": {"remove_keyboard": True},
            })
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": "Нажмите кнопку ниже, чтобы открыть приложение.",
                "reply_markup": {"inline_keyboard": [[{
                    "text": "Открыть «Сейчас»", "web_app": {"url": web_app_url},
                }]]},
            })
        except requests.RequestException:
            app.logger.exception("Telegram webhook reply failed")
    return jsonify(ok=True)


@app.post("/auth/telegram-mini-app")
def telegram_mini_app():
    data = json_body()
    init_data = data.get("init_data", "")
    user_data = verify_mini_app_init_data(init_data)
    if not user_data:
        return jsonify(error="Не удалось подтвердить запуск из Telegram"), 401
    user = upsert_telegram_user(user_data)
    start_param = str(data.get("handoff") or "")[:80]
    if not start_param:
        start_param = dict(parse_qsl(init_data, keep_blank_values=True)).get("start_param", "")
    claimed = claim_invitation(user, start_param) if start_param.startswith("invite_") else False
    handoff_claimed = claim_auth_handoff(user, start_param)
    return jsonify(ok=True, invitation_claimed=claimed, handoff_claimed=handoff_claimed,
                   device_token=issue_device_token(user),
                   user={"id": user.id, "name": user.name, "username": user.username})


@app.post("/auth/handoff")
def create_auth_handoff():
    if not BOT_TOKEN:
        return jsonify(error="Вход через Telegram временно недоступен"), 503
    token = ""
    for _ in range(10):
        candidate = f"{secrets.randbelow(100_000_000):08d}"
        if not AuthHandoff.query.filter_by(token=candidate).first():
            token = candidate
            break
    if not token:
        return jsonify(error="Не удалось создать код. Попробуйте ещё раз"), 503
    db.session.add(AuthHandoff(token=token, expires_at=utcnow() + timedelta(minutes=10)))
    db.session.commit()
    session.permanent = True
    session["auth_handoff"] = token
    return jsonify(
        handoff_token=token,
        login_code=token,
        telegram_url=f"https://t.me/{BOT_USERNAME}",
    ), 201


@app.post("/auth/handoff/<token>")
def complete_auth_handoff(token):
    if not secrets.compare_digest(str(session.get("auth_handoff") or ""), token):
        return jsonify(error="Это подтверждение создано на другом устройстве"), 403
    handoff = AuthHandoff.query.filter_by(token=token).first()
    if not handoff or normalize_dt(handoff.expires_at) <= utcnow():
        session.pop("auth_handoff", None)
        return jsonify(error="Время подтверждения истекло"), 410
    if not handoff.user_id:
        return jsonify(authenticated=False, status="pending"), 202
    user = db.session.get(User, handoff.user_id)
    if not user or handoff.used_at:
        session.pop("auth_handoff", None)
        return jsonify(error="Подтверждение уже использовано"), 410
    handoff.used_at = utcnow()
    db.session.commit()
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    return jsonify(authenticated=True, user={"id": user.id, "name": user.name, "username": user.username})


@app.post("/auth/device")
def device_auth():
    user = restore_device_session(str(json_body().get("device_token") or ""))
    if not user:
        return jsonify(error="Сохранённое подтверждение истекло"), 401
    return jsonify(ok=True, user={"id": user.id, "name": user.name, "username": user.username})


@app.get("/auth/device-token")
@login_required
def device_token():
    return jsonify(device_token=issue_device_token(current_user()))


@app.post("/auth/test")
def test_auth():
    if not ALLOW_TEST_AUTH:
        return jsonify(error="Тестовый вход отключён"), 404
    suffix = str(json_body().get("user", "1"))[:20]
    user = upsert_telegram_user({"id": f"test-{suffix}", "first_name": f"Тест {suffix}"})
    if json_body().get("invite_token"):
        claim_invitation(user, json_body().get("invite_token"))
    return jsonify(ok=True, user={"id": user.id, "name": user.name})


@app.get("/auth/telegram/start")
def telegram_start():
    if not oidc_configured():
        return redirect("/?auth=not_configured")
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    session.update(oidc_state=state, oidc_nonce=nonce, oidc_verifier=verifier)
    params = {
        "client_id": CLIENT_ID, "redirect_uri": CALLBACK_URL, "response_type": "code",
        "scope": "openid profile", "state": state, "nonce": nonce,
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


@app.delete("/api/account")
@login_required
def delete_account():
    user = current_user()
    if json_body().get("confirmation") != "УДАЛИТЬ":
        return jsonify(error="Подтвердите удаление аккаунта"), 400

    user_id = user.id
    proposed_place_ids = [row[0] for row in db.session.query(MeetingPlace.id).filter_by(user_id=user_id).all()]
    if proposed_place_ids:
        MeetingPlaceLocation.query.filter(
            MeetingPlaceLocation.place_id.in_(proposed_place_ids)).delete(synchronize_session=False)
    owned_meeting_ids = [row[0] for row in db.session.query(Meeting.id).filter_by(owner_id=user_id).all()]
    if owned_meeting_ids:
        owned_place_ids = [row[0] for row in db.session.query(MeetingPlace.id).filter(
            MeetingPlace.meeting_id.in_(owned_meeting_ids)
        ).all()]
        if owned_place_ids:
            PlaceVote.query.filter(PlaceVote.place_id.in_(owned_place_ids)).delete(synchronize_session=False)
            MeetingPlaceLocation.query.filter(
                MeetingPlaceLocation.place_id.in_(owned_place_ids)).delete(synchronize_session=False)
        PlaceVote.query.filter(PlaceVote.user_id == user_id).delete(synchronize_session=False)
        MeetingPlace.query.filter(MeetingPlace.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        ChatMessage.query.filter(ChatMessage.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        MeetingFeedback.query.filter(MeetingFeedback.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        MeetingThanks.query.filter(MeetingThanks.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        MeetingEvent.query.filter(MeetingEvent.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        UserReport.query.filter(UserReport.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        PhotoConsent.query.filter(PhotoConsent.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        Interest.query.filter(Interest.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        MeetingState.query.filter(MeetingState.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
        Meeting.query.filter(Meeting.id.in_(owned_meeting_ids)).delete(synchronize_session=False)

    PlaceVote.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    MeetingPlace.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    ChatMessage.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    MeetingFeedback.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    MeetingThanks.query.filter(
        (MeetingThanks.giver_id == user_id) | (MeetingThanks.receiver_id == user_id)
    ).delete(synchronize_session=False)
    MeetingEvent.query.filter((MeetingEvent.user_id == user_id) | (MeetingEvent.target_user_id == user_id)).delete(
        synchronize_session=False)
    UserReport.query.filter((UserReport.reporter_id == user_id) | (UserReport.target_id == user_id)).delete(
        synchronize_session=False)
    UserBlock.query.filter((UserBlock.blocker_id == user_id) | (UserBlock.blocked_id == user_id)).delete(
        synchronize_session=False)
    PhotoConsent.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Interest.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Presence.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    ProfileSelfie.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    UserProfile.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    ActionLog.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    UserModeration.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    InviteAccount.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Invitation.query.filter_by(inviter_id=user_id).delete(synchronize_session=False)
    claimed_invites = Invitation.query.filter_by(claimed_by=user_id).all()
    for invitation in claimed_invites:
        invitation.claimed_by = None
        invitation.claimed_at = None
        invitation.status = "created"
    AuthHandoff.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    UserNotification.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    session.clear()
    return jsonify(ok=True)


@app.get("/api/feed")
def feed():
    user = current_user()
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = min(max(request.args.get("radius", 3, type=float), 1), 12)
    category = request.args.get("category", "")
    time_mode = request.args.get("time", "now")
    now = utcnow()
    result = []
    blocked_ids = set()
    if user:
        blocked_ids = {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=user.id).all()}
        blocked_ids |= {b.blocker_id for b in UserBlock.query.filter_by(blocked_id=user.id).all()}
    proposed_owner_ids = set()
    if user:
        proposed_owner_ids = {
            meeting.owner_id
            for interest in Interest.query.filter_by(user_id=user.id).all()
            if (meeting := db.session.get(Meeting, interest.meeting_id))
            and meeting.description.startswith("Предложение встретиться · ")
            and normalize_dt(meeting.expires_at) > now
        }

    if valid_coordinates(lat, lon):
        presences = Presence.query.filter(Presence.active_until > now).all()
        for presence in presences:
            if time_mode == "hour":
                continue
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
            profile = UserProfile.query.filter_by(user_id=presence.user_id).first()
            selfie = ProfileSelfie.query.filter_by(user_id=presence.user_id).first()
            trust = trust_payload(presence.user_id)
            result.append({
                "kind": "person", "id": presence.id, "icon": CATEGORY_ICONS[presence.category],
                "_owner_id": presence.user_id,
                "name": presence.user.name if user else "Участник рядом", "category": presence.category,
                "description": "Открыт к общению", "distance_km": round(distance, 1),
                "latitude": point[0], "longitude": point[1], "expires_at": normalize_dt(presence.active_until).isoformat(),
                "age": profile.age if profile else None, "gender": profile.gender if profile else None,
                "about": selfie.about if selfie else "", "profile_verified": bool(profile and selfie),
                "interested": presence.user_id in proposed_owner_ids,
                "trust": trust,
            })

        meetings = Meeting.query.filter(Meeting.expires_at > now).order_by(Meeting.id.desc()).all()
        interested_ids = set()
        if user:
            interested_ids = {i.meeting_id for i in Interest.query.filter_by(user_id=user.id).all()}
        for meeting in meetings:
            if user and meeting.owner_id == user.id:
                continue
            starts_at = normalize_dt(meeting.starts_at)
            is_now = starts_at <= now + timedelta(minutes=5)
            if time_mode == "now" and not is_now:
                continue
            if time_mode == "hour" and (is_now or starts_at > now + timedelta(minutes=60)):
                continue
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
            profile = UserProfile.query.filter_by(user_id=meeting.owner_id).first()
            selfie = ProfileSelfie.query.filter_by(user_id=meeting.owner_id).first()
            trust = trust_payload(meeting.owner_id)
            result.append({
                "kind": "meeting", "id": meeting.id, "icon": CATEGORY_ICONS[meeting.category],
                "_owner_id": meeting.owner_id,
                "name": meeting.owner.name if user else "Открытая встреча", "category": meeting.category,
                "description": meeting.description, "format": meeting.format, "distance_km": round(distance, 1),
                "latitude": point[0], "longitude": point[1], "mine": bool(user and meeting.owner_id == user.id),
                "interested": meeting.id in interested_ids, "expires_at": normalize_dt(meeting.expires_at).isoformat(),
                "starts_at": starts_at.isoformat(), "time_mode": "now" if is_now else "hour",
                "starts_in_minutes": max(0, round((starts_at - now).total_seconds() / 60)),
                "age": profile.age if profile else None, "gender": profile.gender if profile else None,
                "about": selfie.about if selfie else "", "profile_verified": bool(profile and selfie),
                "trust": trust,
            })
    unique_people = {}
    for item in result:
        owner_id = item.pop("_owner_id")
        previous = unique_people.get(owner_id)
        if (previous is None
                or (item["kind"] == "meeting" and previous["kind"] == "person")
                or (item["kind"] == previous["kind"] == "meeting" and item["id"] > previous["id"])):
            unique_people[owner_id] = item
    result = list(unique_people.values())
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
    # The status stays active until the user explicitly switches it off.
    # A distant date keeps the existing indexed feed query and old rows compatible.
    presence.active_until = utcnow() + timedelta(days=3650)
    presence.updated_at = utcnow()
    user.latitude, user.longitude = float(lat), float(lon)
    db.session.add(presence)
    db.session.commit()
    return jsonify(ok=True, active_until=None,
                   reminder_due_at=(normalize_dt(presence.updated_at) + timedelta(hours=1)).isoformat())


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
                   active_until=None,
                   reminder_due_at=(normalize_dt(presence.updated_at) + timedelta(hours=1)).isoformat()
                   if active else None,
                   latitude=presence.latitude if active else None,
                   longitude=presence.longitude if active else None)


@app.get("/api/notifications")
@login_required
def list_notifications():
    user = current_user()
    items = UserNotification.query.filter_by(user_id=user.id).order_by(
        UserNotification.id.desc()).limit(100).all()
    return jsonify(
        unread=sum(1 for item in items if not item.read_at),
        items=[{
            "id": item.id,
            "kind": item.kind,
            "text": item.text,
            "read": bool(item.read_at),
            "created_at": normalize_dt(item.created_at).isoformat(),
        } for item in items],
    )


@app.post("/api/notifications/read")
@login_required
def read_notifications():
    UserNotification.query.filter_by(user_id=current_user().id, read_at=None).update(
        {"read_at": utcnow()}, synchronize_session=False)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/meetings")
@login_required
def create_meeting():
    user = current_user()
    data = json_body()
    category = data.get("category")
    description = str(data.get("description", "")).strip()[:180]
    meeting_format = data.get("format", "one")
    time_mode = data.get("time_mode", "now")
    try:
        starts_in_minutes = int(data.get("starts_in_minutes", 0 if time_mode == "now" else 30))
    except (TypeError, ValueError):
        return jsonify(error="Некорректное время встречи"), 400
    lat, lon = data.get("latitude", user.latitude), data.get("longitude", user.longitude)
    if category not in VALID_CATEGORIES or not description:
        return jsonify(error="Выберите занятие и цель встречи"), 400
    if meeting_format not in {"one", "group"}:
        return jsonify(error="Некорректный формат встречи"), 400
    if time_mode not in {"now", "hour"}:
        return jsonify(error="Некорректное время встречи"), 400
    if starts_in_minutes not in {0, 15, 30, 45, 60}:
        return jsonify(error="Выберите время от 15 минут до часа"), 400
    if time_mode == "now" and starts_in_minutes != 0:
        return jsonify(error="Для встречи прямо сейчас время должно быть нулевым"), 400
    if time_mode == "hour" and starts_in_minutes == 0:
        return jsonify(error="Выберите время встречи"), 400
    if not valid_coordinates(lat, lon):
        return jsonify(error="Разрешите геолокацию для создания встречи"), 400
    active_owned = Meeting.query.filter(Meeting.owner_id == user.id, Meeting.expires_at > utcnow()).all()
    for previous in active_owned:
        if Interest.query.filter_by(meeting_id=previous.id, status="accepted").first():
            return jsonify(error="Сначала завершите или отмените текущую подтверждённую встречу"), 409
    for previous in active_owned:
        previous.expires_at = utcnow()
        Interest.query.filter_by(meeting_id=previous.id, status="pending").update(
            {"status": "rejected"}, synchronize_session=False
        )
        previous_state = MeetingState.query.filter_by(meeting_id=previous.id).first()
        if previous_state:
            previous_state.status = "cancelled"
    if not consume_action(user.id, "meeting", 5, 3600):
        db.session.rollback()
        return jsonify(error="Слишком много встреч за час. Попробуйте позже"), 429
    starts_at = utcnow() + timedelta(minutes=starts_in_minutes)
    meeting = Meeting(
        owner_id=user.id, category=category, description=description, format=meeting_format,
        latitude=float(lat), longitude=float(lon), starts_at=starts_at,
        expires_at=starts_at + timedelta(minutes=60),
    )
    user.latitude, user.longitude = float(lat), float(lon)
    db.session.add(meeting)
    db.session.commit()
    return jsonify(ok=True, id=meeting.id), 201


@app.post("/api/presences/<int:presence_id>/interest")
@login_required
def propose_meeting_to_presence(presence_id):
    user = current_user()
    presence = db.get_or_404(Presence, presence_id)
    if normalize_dt(presence.active_until) <= utcnow():
        return jsonify(error="Этот человек уже не открыт для встречи"), 409
    if presence.user_id == user.id:
        return jsonify(error="Это ваш статус"), 400
    if (UserBlock.query.filter_by(blocker_id=user.id, blocked_id=presence.user_id).first() or
            UserBlock.query.filter_by(blocker_id=presence.user_id, blocked_id=user.id).first()):
        return jsonify(error="Предложение этому человеку недоступно"), 403
    if not consume_action(user.id, "interest", 12, 3600):
        return jsonify(error="Слишком много откликов за час. Попробуйте позже"), 429
    description = f"Предложение встретиться · {CATEGORY_MEETING_TITLES[presence.category]}"
    existing = (Interest.query.join(Meeting, Interest.meeting_id == Meeting.id)
                .filter(Interest.user_id == user.id,
                        Meeting.owner_id == presence.user_id,
                        Meeting.description == description,
                        Meeting.expires_at > utcnow()).first())
    if existing:
        return jsonify(ok=True, meeting_id=existing.meeting_id, already_sent=True)
    meeting = Meeting(
        owner_id=presence.user_id, category=presence.category, description=description,
        format="one", latitude=presence.latitude, longitude=presence.longitude,
        starts_at=utcnow(), expires_at=min(normalize_dt(presence.active_until), utcnow() + timedelta(minutes=60)),
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(Interest(meeting_id=meeting.id, user_id=user.id))
    db.session.commit()
    notify_user(presence.user_id,
                f"{user.name} предлагает встретиться: {CATEGORY_MEETING_TITLES[presence.category]}")
    return jsonify(ok=True, meeting_id=meeting.id, already_sent=False), 201


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
    if meeting.format == "group" and accepted_count >= 5:
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
            # Selfies are never exposed here; the meeting room applies the owner's visibility choice.
            "picture": None,
            "username": None,
        },
        "owner": {
            "name": meeting.owner.name,
            "picture": None,
            "username": None,
        },
        "can_decide": viewer.id == meeting.owner_id and interest.status == "pending",
    }


@app.get("/api/interests")
@login_required
def list_interests():
    user = current_user()
    active_meetings = Meeting.query.filter(Meeting.expires_at > utcnow()).all()
    active_meeting_ids = [row.id for row in active_meetings]
    owned_meetings = [row for row in active_meetings if row.owner_id == user.id]
    owned_meeting_ids = [row.id for row in owned_meetings]
    incoming = (Interest.query.filter(Interest.meeting_id.in_(owned_meeting_ids)).all()
                if owned_meeting_ids else [])
    outgoing = (Interest.query.filter(Interest.user_id == user.id,
                                      Interest.meeting_id.in_(active_meeting_ids)).all()
                if active_meeting_ids else [])
    return jsonify(
        owned=[{
            "meeting_id": meeting.id,
            "description": meeting.description,
            "category": meeting.category,
            "format": meeting.format,
            "status": "owned",
            "accepted_count": Interest.query.filter_by(
                meeting_id=meeting.id, status="accepted"
            ).count(),
            "pending_count": Interest.query.filter_by(
                meeting_id=meeting.id, status="pending"
            ).count(),
        } for meeting in owned_meetings],
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
        if accepted_count > 5:
            db.session.rollback()
            return jsonify(error="В группе может быть не больше 6 человек вместе с создателем"), 409
    db.session.commit()
    result_text = "принят" if decision == "accepted" else "отклонён"
    notify_user(interest.user_id, f"Ваш отклик на встречу «{meeting.description}» {result_text}")
    return jsonify(ok=True, interest=interest_payload(interest, user))


def meeting_member(meeting, user):
    if meeting.owner_id == user.id:
        return True
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
    raw_events = MeetingEvent.query.filter_by(meeting_id=meeting.id).order_by(MeetingEvent.id.desc()).all()
    event_keys, events = set(), []
    for event in raw_events:
        key = (event.kind, event.user_id, event.target_user_id)
        if key not in event_keys:
            event_keys.add(key)
            events.append(event)
    events.reverse()
    feedback = MeetingFeedback.query.filter_by(meeting_id=meeting.id).order_by(MeetingFeedback.id).all()
    thanks = MeetingThanks.query.filter_by(meeting_id=meeting.id).all()
    member_ids = accepted_user_ids(meeting)
    consented_ids = {row.user_id for row in PhotoConsent.query.filter_by(meeting_id=meeting.id).all()}
    photos_revealed = bool(member_ids) and member_ids.issubset(consented_ids)
    participant_payload = []
    for uid in member_ids:
        participant = db.session.get(User, uid)
        selfie = ProfileSelfie.query.filter_by(user_id=uid).first()
        visibility = selfie.visibility if selfie else "hidden"
        photo_visible = bool(selfie and (
            uid == user.id or visibility == "accepted" or (visibility == "mutual" and photos_revealed)
        ))
        participant_payload.append({
            "id": uid, "name": participant.name, "mine": uid == user.id,
            "picture": decrypt_selfie(selfie.image) if photo_visible else None,
            "photo_visible": photo_visible, "photo_visibility": visibility,
            "photo_consented": uid in consented_ids,
            "trust": trust_payload(uid),
            "thanked_by_me": any(row.giver_id == user.id and row.receiver_id == uid for row in thanks),
        })
    my_late = any(event.kind == "late" and event.user_id == user.id for event in events)
    return {"meeting": {"id": meeting.id, "description": meeting.description, "format": meeting.format,
                         "latitude": meeting.latitude, "longitude": meeting.longitude,
                         "is_owner": meeting.owner_id == user.id, "status": state.status if state else "active",
                         "my_late": my_late},
            "places": [{
                "id": p.id,
                "title": p.title,
                "votes": vote_counts.get(p.id, 0),
                "voted": p.id in my_votes,
                "confirmed": bool(p.confirmed),
                "latitude": location.latitude if (location := MeetingPlaceLocation.query.filter_by(
                    place_id=p.id).first()) else None,
                "longitude": location.longitude if location else None,
                "map_url": (
                    f"https://www.openstreetmap.org/?mlat={location.latitude:.6f}"
                    f"&mlon={location.longitude:.6f}#map=17/{location.latitude:.6f}/{location.longitude:.6f}"
                ) if location else None,
            } for p in places],
            "messages": [{"id": m.id, "name": db.session.get(User, m.user_id).name, "text": m.text,
                          "mine": m.user_id == user.id, "created_at": normalize_dt(m.created_at).isoformat()}
                         for m in messages],
            "events": [{"kind": e.kind, "name": db.session.get(User, e.user_id).name,
                        "note": e.note or ""} for e in events],
            "traces": [{"name": db.session.get(User, f.user_id).name, "text": f.trace} for f in feedback],
            "thanks": [{"giver_id": row.giver_id, "receiver_id": row.receiver_id} for row in thanks],
            "participants": participant_payload,
            "photos_revealed": photos_revealed,
            "my_photo_consent": user.id in consented_ids,
            "mutual_photo_used": any(p["photo_visibility"] == "mutual" for p in participant_payload)}


@app.get("/api/meetings/<int:meeting_id>/room")
@login_required
def get_meeting_room(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    return error or jsonify(room_payload(meeting, current_user()))


@app.post("/api/meetings/<int:meeting_id>/photo-consent")
@login_required
def consent_to_photo(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    user = current_user()
    consent = PhotoConsent.query.filter_by(meeting_id=meeting.id, user_id=user.id).first()
    if not consent:
        db.session.add(PhotoConsent(meeting_id=meeting.id, user_id=user.id))
        db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, user))


@app.post("/api/meetings/<int:meeting_id>/places")
@login_required
def propose_place(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    title = str(json_body().get("title", "")).strip()[:120]
    if len(title) < 2:
        return jsonify(error="Укажите место встречи"), 400
    latitude = json_body().get("latitude")
    longitude = json_body().get("longitude")
    has_coordinates = latitude is not None or longitude is not None
    if has_coordinates and not valid_coordinates(latitude, longitude):
        return jsonify(error="Некорректная точка на карте"), 400
    place = MeetingPlace(meeting_id=meeting.id, user_id=current_user().id, title=title)
    db.session.add(place)
    db.session.flush()
    if has_coordinates:
        db.session.add(MeetingPlaceLocation(
            place_id=place.id, latitude=float(latitude), longitude=float(longitude), source="map",
        ))
    db.session.commit()
    return jsonify(ok=True, room=room_payload(meeting, current_user())), 201


@app.post("/api/geocode/reverse")
@login_required
def reverse_geocode():
    data = json_body()
    if not valid_coordinates(data.get("latitude"), data.get("longitude")):
        return jsonify(error="Некорректная точка на карте"), 400
    if not consume_action(current_user().id, "geocode", 30, 3600):
        return jsonify(error="Слишком много запросов к карте. Попробуйте позже"), 429
    latitude, longitude = float(data["latitude"]), float(data["longitude"])
    coordinate_key = f"{latitude:.5f},{longitude:.5f}"
    cached = GeocodeCache.query.filter_by(coordinate_key=coordinate_key).first()
    if cached:
        return jsonify(title=cached.title, latitude=latitude, longitude=longitude)
    title = "Точка на карте"
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": latitude, "lon": longitude, "format": "jsonv2", "accept-language": "ru"},
            headers={"User-Agent": f"Sichas-Minsk/0.15 ({PUBLIC_URL}; dudicoffnet@gmail.com)"},
            timeout=6,
        )
        response.raise_for_status()
        payload = response.json()
        address = payload.get("address") or {}
        title = (
            payload.get("name")
            or address.get("amenity")
            or address.get("shop")
            or address.get("leisure")
            or address.get("road")
            or payload.get("display_name")
            or title
        )
        if title == address.get("road"):
            house_number = address.get("house_number")
            title = f"{title}, {house_number}" if house_number else title
    except (requests.RequestException, ValueError):
        app.logger.warning("Reverse geocoding failed for %s", coordinate_key)
    title = str(title).strip()[:120] or "Точка на карте"
    db.session.add(GeocodeCache(coordinate_key=coordinate_key, title=title))
    db.session.commit()
    return jsonify(title=title, latitude=latitude, longitude=longitude)


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
    if action not in {"late", "cancel", "complete", "no_show", "leave"}:
        return jsonify(error="Неизвестное действие"), 400
    state = state_for(meeting)
    if state.status != "active":
        return jsonify(error="Встреча уже завершена или отменена"), 409
    target_id = data.get("target_user_id")
    if action in {"cancel", "complete"} and meeting.owner_id != user.id:
        return jsonify(error="Это действие доступно создателю встречи"), 403
    if action == "leave":
        if meeting.owner_id == user.id:
            return jsonify(error="Создатель может отменить встречу"), 400
        interest = Interest.query.filter_by(
            meeting_id=meeting.id, user_id=user.id, status="accepted"
        ).first()
        if not interest:
            return jsonify(error="Вы уже не участвуете в этой встрече"), 409
        interest.status = "rejected"
        db.session.add(MeetingEvent(meeting_id=meeting.id, user_id=user.id,
                                    kind="leave", note="Участник отказался от встречи"))
        db.session.commit()
        notify_user(meeting.owner_id, f"{user.name} отказался от встречи «{meeting.description}»")
        return jsonify(ok=True, left=True)
    if action == "no_show":
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return jsonify(error="Укажите участника, который не пришёл"), 400
        if target_id == user.id or target_id not in accepted_user_ids(meeting):
            return jsonify(error="Некорректный участник"), 400
    if action == "cancel":
        state.status = "cancelled"
        meeting.expires_at = utcnow()
    elif action == "complete":
        state.status = "completed"
        meeting.expires_at = utcnow()
        reward_completed_invites(accepted_user_ids(meeting))
    elif action == "late":
        existing_late = MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=user.id, kind="late"
        ).all()
        if existing_late:
            for event in existing_late:
                db.session.delete(event)
            db.session.commit()
            return jsonify(ok=True, room=room_payload(meeting, user))
    elif action == "no_show":
        MeetingEvent.query.filter_by(meeting_id=meeting.id, user_id=user.id,
                                     target_user_id=target_id, kind="no_show").delete()
    db.session.add(MeetingEvent(meeting_id=meeting.id, user_id=user.id, target_user_id=target_id,
                                kind=action, note=str(data.get("note", "")).strip()[:180] or None))
    db.session.commit()
    if action in {"cancel", "complete", "late", "no_show"}:
        action_text = {
            "cancel": "Встреча отменена создателем",
            "complete": "Встреча завершена — можно оставить благодарность и короткий след",
            "late": f"{user.name} сообщает, что опаздывает",
            "no_show": "По встрече отмечена неявка участника",
        }[action]
        for member_id in accepted_user_ids(meeting) - {user.id}:
            notify_user(member_id, f"{action_text}: «{meeting.description}»", kind=f"meeting_{action}")
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


@app.post("/api/meetings/<int:meeting_id>/thanks")
@login_required
def thank_participant(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if not state or state.status != "completed":
        return jsonify(error="Поблагодарить можно после завершения встречи"), 409
    user = current_user()
    try:
        receiver_id = int(json_body().get("target_user_id"))
    except (TypeError, ValueError):
        return jsonify(error="Укажите участника"), 400
    if receiver_id == user.id or receiver_id not in accepted_user_ids(meeting):
        return jsonify(error="Некорректный участник"), 400
    thanks = MeetingThanks.query.filter_by(
        meeting_id=meeting.id, giver_id=user.id, receiver_id=receiver_id).first()
    if not thanks:
        db.session.add(MeetingThanks(
            meeting_id=meeting.id, giver_id=user.id, receiver_id=receiver_id,
        ))
        db.session.commit()
        notify_user(receiver_id, f"{user.name} поблагодарил вас после встречи «{meeting.description}»",
                    kind="thanks",
                    dedupe_key=f"thanks:{meeting.id}:{user.id}:{receiver_id}")
    return jsonify(ok=True, room=room_payload(meeting, user))


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


@app.get("/api/admin/traffic")
@login_required
def admin_traffic():
    user = current_user()
    if user.telegram_id not in ADMIN_TELEGRAM_IDS:
        return jsonify(error="Нет доступа"), 403
    try:
        days = min(max(int(request.args.get("days", 30)), 1), 90)
    except ValueError:
        days = 30
    visits = TrafficVisit.query.filter(
        TrafficVisit.created_at >= utcnow() - timedelta(days=days)
    ).order_by(TrafficVisit.id.desc()).all()
    grouped = {}
    for visit in visits:
        key = (visit.source, visit.medium, visit.campaign)
        item = grouped.setdefault(key, {
            "source": visit.source,
            "medium": visit.medium,
            "campaign": visit.campaign,
            "visits": 0,
            "unique_visitors": set(),
        })
        item["visits"] += 1
        item["unique_visitors"].add(visit.visitor_hash)
    items = [{**item, "unique_visitors": len(item["unique_visitors"])} for item in grouped.values()]
    items.sort(key=lambda item: (-item["visits"], item["source"]))
    return jsonify(days=days, total_visits=len(visits),
                   unique_visitors=len({visit.visitor_hash for visit in visits}), items=items)


@app.get("/api/invitations")
@login_required
def list_invitations():
    user = current_user()
    account = invite_account(user.id)
    db.session.commit()
    items = Invitation.query.filter_by(inviter_id=user.id).order_by(Invitation.id.desc()).all()
    rewarded = Invitation.query.filter_by(inviter_id=user.id, status="rewarded").count()
    return jsonify(available=account.available, rewarded=rewarded,
                   develops_club=rewarded > 0,
                   items=[{"url": f"https://t.me/{BOT_USERNAME}?startapp=invite_{item.token}",
                           "status": item.status} for item in items])


@app.post("/api/invitations")
@login_required
def create_invitation():
    user = current_user()
    account = invite_account(user.id)
    if account.available <= 0:
        return jsonify(error="Приглашения закончились. Оно вернётся после успешной встречи приглашённого"), 409
    token = secrets.token_urlsafe(12)
    account.available -= 1
    invitation = Invitation(token=token, inviter_id=user.id, status="created")
    db.session.add(invitation)
    db.session.commit()
    return jsonify(ok=True, available=account.available,
                   url=f"https://t.me/{BOT_USERNAME}?startapp=invite_{token}"), 201


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/sichas-apple-icon-v2.png")
def ios_app_icon():
    response = send_from_directory(BASE_DIR, "apple-touch-icon.png")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/<path:path>")
def static_files(path):
    full_path = os.path.join(BASE_DIR, path)
    if os.path.isfile(full_path):
        return send_from_directory(BASE_DIR, path)
    return send_from_directory(BASE_DIR, "index.html")


with app.app_context():
    db.create_all()
    cleared_phones = User.query.filter(User.phone_number.isnot(None)).update(
        {"phone_number": None}, synchronize_session=False
    )
    legacy_selfies = ProfileSelfie.query.filter(~ProfileSelfie.image.startswith("enc:")).all()
    if legacy_selfies or cleared_phones:
        for legacy_selfie in legacy_selfies:
            legacy_selfie.image = encrypt_selfie(legacy_selfie.image)
        db.session.commit()
        app.logger.info("Encrypted %s legacy selfies and cleared %s phone values",
                        len(legacy_selfies), cleared_phones)

if BOT_TOKEN and os.getenv("DATABASE_URL"):
    threading.Thread(target=presence_reminder_loop, name="presence-reminders", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
