import base64
from collections import OrderedDict
import hashlib
import io
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import jwt
import requests
import qrcode
from PIL import Image, UnidentifiedImageError
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    has_request_context,
    Flask, Response, abort, g, jsonify, redirect, request, send_from_directory, session,
    stream_with_context,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, distinct, func, literal, select, text as sql_text,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.0.0-rc.1"
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Minsk"))
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"} or bool(os.getenv("RAILWAY_ENVIRONMENT"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
CLIENT_ID = os.getenv("TELEGRAM_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TELEGRAM_CLIENT_SECRET", "").strip()
CALLBACK_URL = f"{PUBLIC_URL}/auth/telegram/callback"
ALLOW_TEST_AUTH = os.getenv("ALLOW_TEST_AUTH", "false").lower() == "true"
ENABLE_SSE = os.getenv(
    "ENABLE_SSE", "true" if ALLOW_TEST_AUTH and not IS_PRODUCTION else "false"
).lower() == "true"
ADMIN_TELEGRAM_IDS = {value.strip() for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if value.strip()}
BOT_USERNAME = os.getenv("BOT_USERNAME", "vmeste_rjadom_bot").strip().lstrip("@")
EXPECTED_SCHEMA_REVISION = "0010_release_controls"

if IS_PRODUCTION:
    missing_configuration = [
        name for name, value in {
            "SECRET_KEY": os.getenv("SECRET_KEY"),
            "SELFIE_ENCRYPTION_KEY": os.getenv("SELFIE_ENCRYPTION_KEY"),
            "DATABASE_URL": os.getenv("DATABASE_URL"),
            "PUBLIC_URL": os.getenv("PUBLIC_URL"),
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "ADMIN_TELEGRAM_IDS": os.getenv("ADMIN_TELEGRAM_IDS"),
        }.items() if not str(value or "").strip()
    ]
    if missing_configuration:
        raise RuntimeError(
            "Missing required production configuration: "
            + ", ".join(sorted(missing_configuration))
        )
    public_url = urlparse(PUBLIC_URL)
    invalid_configuration = []
    if public_url.scheme != "https" or not public_url.netloc:
        invalid_configuration.append("PUBLIC_URL must be an absolute HTTPS URL")
    if not str(os.getenv("DATABASE_URL") or "").startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        invalid_configuration.append("DATABASE_URL must use PostgreSQL in production")
    if ALLOW_TEST_AUTH:
        invalid_configuration.append("ALLOW_TEST_AUTH must be false")
    if any(not value.isdigit() or int(value) <= 0 for value in ADMIN_TELEGRAM_IDS):
        invalid_configuration.append("ADMIN_TELEGRAM_IDS must contain positive numeric Telegram IDs")
    if invalid_configuration:
        raise RuntimeError("Invalid production configuration: " + "; ".join(invalid_configuration))

database_url = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(BASE_DIR, 'sichas.db')}"
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]

secret_seed = os.getenv("SECRET_KEY") or CLIENT_SECRET or BOT_TOKEN
if not secret_seed:
    secret_seed = secrets.token_hex(32)
selfie_key_seed = os.getenv("SELFIE_ENCRYPTION_KEY") or f"{secret_seed}|development-selfie-key"
SELFIE_ENCRYPTION_KEY_VERSION = (
    os.getenv("SELFIE_ENCRYPTION_KEY_VERSION", "v1").strip() or "v1"
)

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = hashlib.sha256(f"{secret_seed}|seichas-session".encode()).hexdigest()
selfie_cipher = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(selfie_key_seed.encode()).digest()
))
app.config.update(
    SESSION_COOKIE_SECURE=PUBLIC_URL.startswith("https://") and not app.testing,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=2_200_000,
)


class StructuredLogFormatter(logging.Formatter):
    """Emit operational metadata without request bodies, query strings, or credentials."""

    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


if IS_PRODUCTION:
    for handler in app.logger.handlers:
        handler.setFormatter(StructuredLogFormatter())
device_token_serializer = URLSafeTimedSerializer(app.secret_key, salt="sichas-device-auth-v1")
DEVICE_TOKEN_MAX_AGE = 180 * 24 * 60 * 60
GEOCODE_LOCK = threading.Lock()
last_geocode_request_at = 0.0
REALTIME_CONDITION = threading.Condition()
REALTIME_REVISIONS = {}
REALTIME_EVENTS = {}


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
    return f"enc:{SELFIE_ENCRYPTION_KEY_VERSION}:" + selfie_cipher.encrypt(value.encode()).decode()


def decrypt_selfie(value):
    if not value:
        return None
    if not value.startswith("enc:"):
        return value
    try:
        encrypted_value = value[4:]
        if ":" in encrypted_value:
            version, encrypted_value = encrypted_value.split(":", 1)
            if version != SELFIE_ENCRYPTION_KEY_VERSION:
                app.logger.error("Stored selfie uses an unavailable encryption key version")
                return None
            return selfie_cipher.decrypt(encrypted_value.encode()).decode()
        # 0.17.8 encrypted unversioned selfies with a key derived from the
        # original session secret. Production migrations rewrite them.
        legacy_cipher = Fernet(base64.urlsafe_b64encode(
            hashlib.sha256(f"{app.secret_key}|selfie-storage-v1".encode()).digest()
        ))
        return legacy_cipher.decrypt(encrypted_value.encode()).decode()
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
    visibility = db.Column(db.String(20), nullable=False, default="hidden")
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
    category = db.Column(db.String(40), nullable=False, default="other", index=True)
    reason = db.Column(db.String(180), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="new", index=True)
    moderator_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    decision = db.Column(db.String(40), index=True)
    decision_reason = db.Column(db.String(180))
    auto_hidden = db.Column(db.Integer, nullable=False, default=0)
    reviewed_at = db.Column(db.DateTime(timezone=True))
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
    hidden_until = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
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


class SyncRevision(db.Model):
    __tablename__ = "sync_revision"
    key = db.Column(db.String(120), primary_key=True)
    revision = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ModerationAction(db.Model):
    __tablename__ = "moderation_action"
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("user_report.id"), index=True)
    moderator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    action = db.Column(db.String(40), nullable=False, index=True)
    reason = db.Column(db.String(180))
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class NotificationOutbox(db.Model):
    __tablename__ = "notification_outbox"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    dedupe_key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    payload_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    delivered_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.String(180))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class OperationRecord(db.Model):
    __tablename__ = "operation_record"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    operation_id = db.Column(db.String(80), nullable=False)
    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(180), nullable=False)
    status_code = db.Column(db.Integer, nullable=False)
    response_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    __table_args__ = (UniqueConstraint("user_id", "operation_id", name="uq_operation_user_id"),)


SAFE_HELP_GOALS = {
    "Обсудить вопрос дистанционно",
    "Подсказать дорогу в публичном месте",
    "Помочь разобраться в городской навигации",
    "Помочь друг другу",
}
VALID_CATEGORIES = {"cafe", "walk", "talk", "active", "shop", "help", "leisure"}
REPORT_CATEGORIES = {"safety", "harassment", "spam", "no_show", "no_show_dispute", "other"}
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


def bump_revision_keys(*keys):
    """Atomically increment durable revisions in the caller's transaction."""
    now = utcnow()
    for key in sorted({str(key)[:120] for key in keys if key}):
        values = {"key": key, "revision": 1, "updated_at": now}
        if engine.dialect.name == "postgresql":
            statement = postgresql_insert(SyncRevision).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[SyncRevision.key],
                set_={"revision": SyncRevision.revision + 1, "updated_at": now},
            )
        else:
            statement = sqlite_insert(SyncRevision).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[SyncRevision.key],
                set_={"revision": SyncRevision.revision + 1, "updated_at": now},
            )
        db.session.execute(statement)


def bump_sync(feed=False, user_ids=(), meeting_ids=(), notification_ids=(), admin=False):
    keys = []
    if feed:
        keys.append("feed")
    if admin:
        keys.append("admin")
    keys.extend(f"user:{user_id}" for user_id in set(user_ids))
    keys.extend(f"room:{meeting_id}" for meeting_id in set(meeting_ids))
    keys.extend(f"notifications:{user_id}" for user_id in set(notification_ids))
    bump_revision_keys(*keys)


def revision_value(key):
    row = db.session.get(SyncRevision, key)
    return row.revision if row else 0


def request_operation_id():
    value = str(request.headers.get("X-Operation-ID") or json_body().get("operation_id") or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,80}", value):
        return ""
    return value


def replay_operation(user, operation_id):
    if not operation_id:
        return None
    record = OperationRecord.query.filter_by(user_id=user.id, operation_id=operation_id).first()
    if not record:
        return None
    if record.method != request.method or record.path != request.path:
        return jsonify(error="Operation ID уже использован для другого действия",
                       code="OPERATION_ID_CONFLICT"), 409
    payload = json.loads(record.response_json)
    meeting_id = payload.pop("_room_meeting_id", None)
    previous_room = payload.pop("room", None)
    if previous_room:
        meeting_id = previous_room["meeting"]["id"]
    if meeting_id:
        meeting = db.session.get(Meeting, meeting_id)
        if meeting and meeting_member(meeting, user) and not chat_closed_for(meeting, user.id):
            payload["room"] = room_payload(meeting, user)
    payload["replayed"] = True
    return jsonify(payload), record.status_code


def remember_operation(user, operation_id, payload, status_code):
    if operation_id:
        db.session.add(OperationRecord(
            user_id=user.id,
            operation_id=operation_id,
            method=request.method,
            path=request.path,
            status_code=status_code,
            response_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ))


def admin_user():
    user = current_user()
    return user if user and user.telegram_id in ADMIN_TELEGRAM_IDS else None


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not admin_user():
            return jsonify(error="Нет доступа"), 403
        return view(*args, **kwargs)
    return wrapped


def admin_csrf_token():
    if not session.get("admin_csrf"):
        session["admin_csrf"] = secrets.token_urlsafe(32)
    return session["admin_csrf"]


def verify_admin_csrf():
    received = request.headers.get("X-CSRF-Token", "")
    expected = str(session.get("admin_csrf") or "")
    if not expected or not secrets.compare_digest(received, expected):
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return not IS_PRODUCTION
    allowed = {urlparse(PUBLIC_URL).netloc, urlparse(request.host_url).netloc}
    return urlparse(origin).netloc in allowed


def anonymized_user_id(user_id):
    return "u_" + hmac.new(
        app.secret_key.encode(), f"admin-user:{user_id}".encode(), hashlib.sha256
    ).hexdigest()[:12]


def independent_report_count(target_id, since=None):
    query = db.session.query(func.count(distinct(UserReport.reporter_id))).filter(
        UserReport.target_id == target_id,
        UserReport.category != "no_show_dispute",
    )
    if since:
        query = query.filter(UserReport.created_at >= since)
    return int(query.scalar() or 0)


def queue_admin_report(report, report_count):
    for telegram_id in sorted(ADMIN_TELEGRAM_IDS):
        payload = {
            "report_id": report.id,
            "category": report.category,
            "created_at": normalize_dt(report.created_at).isoformat(),
            "report_count": report_count,
            "status": report.status,
            "admin_telegram_id": telegram_id,
        }
        db.session.add(NotificationOutbox(
            kind="admin_report",
            dedupe_key=f"admin-report:{report.id}:{telegram_id}",
            payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            status="pending",
            next_attempt_at=utcnow(),
        ))


def process_notification_outbox(limit=20):
    now = utcnow()
    query = NotificationOutbox.query.filter(
        NotificationOutbox.kind == "admin_report",
        NotificationOutbox.status.in_(("pending", "failed")),
        NotificationOutbox.next_attempt_at <= now,
    ).order_by(NotificationOutbox.id).limit(limit)
    if engine.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = query.all()
    for row in rows:
        row.status = "sending"
        row.attempts += 1
        try:
            payload = json.loads(row.payload_json)
            if row.kind != "admin_report":
                raise ValueError("Unsupported outbox event")
            message = (
                f"Новая жалоба #{payload['report_id']}\n"
                f"Категория: {payload['category']}\n"
                f"Дата: {payload['created_at']}\n"
                f"Жалоб на пользователя: {payload['report_count']}\n"
                f"Статус: {payload['status']}"
            )
            recipients = [payload["admin_telegram_id"]] if payload.get("admin_telegram_id") else sorted(ADMIN_TELEGRAM_IDS)
            for telegram_id in recipients:
                if telegram_id not in ADMIN_TELEGRAM_IDS:
                    continue
                telegram_api("sendMessage", {
                    "chat_id": telegram_id,
                    "text": message,
                    "reply_markup": {"inline_keyboard": [[{
                        "text": "Открыть жалобу",
                        "url": f"{PUBLIC_URL}/admin?report={payload['report_id']}",
                    }]]},
                })
            row.status = "sent"
            row.delivered_at = utcnow()
            row.last_error = None
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            row.status = "failed"
            row.last_error = type(error).__name__[:180]
            row.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 10)))
            app.logger.warning("Admin report notification delivery failed for outbox %s", row.id)
    db.session.commit()
    return len(rows)


def notification_outbox_loop():
    while True:
        time.sleep(5)
        try:
            with app.app_context():
                process_notification_outbox()
                process_user_telegram_outbox()
        except Exception:
            record_runtime_error("background_errors")
            app.logger.exception("Notification outbox worker failed")


def lock_user_actions(user_ids):
    if engine.dialect.name == "postgresql":
        for user_id in sorted(set(user_ids)):
            db.session.execute(sql_text("SELECT pg_advisory_xact_lock(:user_id)"), {"user_id": int(user_id)})


def active_confirmed_meeting_ids(user_id, exclude_meeting_id=None):
    candidate_ids = {
        row.id for row in Meeting.query.filter_by(owner_id=user_id).all()
        if Interest.query.filter_by(meeting_id=row.id, status="accepted").first()
    }
    candidate_ids.update(
        row.meeting_id for row in Interest.query.filter_by(user_id=user_id, status="accepted").all()
    )
    result = set()
    for meeting_id in candidate_ids:
        if meeting_id == exclude_meeting_id:
            continue
        meeting = db.session.get(Meeting, meeting_id)
        if meeting and effective_meeting_status(meeting) == "active":
            result.add(meeting_id)
    return result


def emit_realtime(user_ids, kind="changed", meeting_id=None):
    """Wake connected clients after the database transaction is committed."""
    payload = {"kind": kind, "meeting_id": meeting_id, "at": int(time.time())}
    with REALTIME_CONDITION:
        for user_id in set(user_ids):
            revision = REALTIME_REVISIONS.get(user_id, 0) + 1
            REALTIME_REVISIONS[user_id] = revision
            REALTIME_EVENTS[user_id] = {**payload, "revision": revision}
        REALTIME_CONDITION.notify_all()


def current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def completed_profile(user):
    return bool(user and (ALLOW_TEST_AUTH and user.telegram_id.startswith("test-") or (
        UserProfile.query.filter_by(user_id=user.id).first()
        and ProfileSelfie.query.filter_by(user_id=user.id).first()
    )))


def profile_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not completed_profile(current_user()):
            return jsonify(error="Сначала заполните профиль"), 403
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify(error="Нужна регистрация через Telegram"), 401
        return view(*args, **kwargs)
    return wrapped


@app.get("/api/events")
@login_required
def realtime_events():
    if not ENABLE_SSE:
        return jsonify(error="SSE отключён; используйте /api/sync-state"), 404
    user_id = current_user().id

    @stream_with_context
    def stream():
        revision = REALTIME_REVISIONS.get(user_id, 0)
        yield "retry: 1500\n\n"
        while True:
            with REALTIME_CONDITION:
                REALTIME_CONDITION.wait_for(
                    lambda: REALTIME_REVISIONS.get(user_id, 0) != revision,
                    timeout=15,
                )
                next_revision = REALTIME_REVISIONS.get(user_id, 0)
                event = REALTIME_EVENTS.get(user_id)
            if next_revision != revision and event:
                revision = next_revision
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                yield ": heartbeat\n\n"

    response = Response(stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def json_body():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


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
    token = str(token or "").removeprefix("invite_")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16}", token):
        return False
    transaction_lock(User.query.filter_by(id=user.id), User.id)
    invitation = Invitation.query.filter_by(token=token, status="created").with_for_update().first()
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


def completed_by_all(meeting):
    member_ids = accepted_user_ids(meeting)
    if len(member_ids) < 2:
        return False
    confirmed_ids = {
        row.user_id for row in MeetingEvent.query.filter_by(
            meeting_id=meeting.id, kind="complete"
        ).all()
    }
    return member_ids.issubset(confirmed_ids)


def feedback_rating(feedback):
    if not feedback:
        return None
    match = re.fullmatch(r"rating:([1-5])", feedback.trace or "")
    return int(match.group(1)) if match else None


def meeting_has_report(meeting_id):
    evidence_since = utcnow() - timedelta(days=30)
    return UserReport.query.filter(
        UserReport.meeting_id == meeting_id,
        UserReport.created_at >= evidence_since,
    ).first() is not None


def chat_cleanup_deadline(meeting):
    terminal_events = (MeetingEvent.query.filter(
        MeetingEvent.meeting_id == meeting.id,
        MeetingEvent.kind.in_(("complete", "leave")),
    ).order_by(MeetingEvent.created_at).all())
    dates = [normalize_dt(event.created_at) for event in terminal_events]
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if state and state.status == "cancelled":
        dates.append(normalize_dt(state.updated_at))
    return min(dates) + timedelta(hours=24) if dates else None


def chat_expired(meeting):
    deadline = chat_cleanup_deadline(meeting)
    return bool(deadline and deadline <= utcnow())


def chat_closed_for(meeting, user_id):
    if MeetingFeedback.query.filter_by(meeting_id=meeting.id, user_id=user_id).first():
        return True
    if UserReport.query.filter(
        UserReport.meeting_id == meeting.id,
        UserReport.reporter_id == user_id,
        UserReport.category != "no_show_dispute",
    ).first():
        return True
    if MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=user_id, kind="leave"
    ).first():
        return True
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    return bool((state and state.status == "cancelled") or chat_expired(meeting))


def purge_chat_if_ready(meeting):
    """Delete chat text, unless a complaint requires evidence for moderation."""
    if meeting_has_report(meeting.id):
        return False
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    should_purge = bool(state and state.status == "cancelled")
    should_purge = should_purge or MeetingEvent.query.filter_by(
        meeting_id=meeting.id, kind="leave"
    ).first() is not None
    member_ids = accepted_user_ids(meeting)
    rated_ids = {
        row.user_id for row in MeetingFeedback.query.filter_by(meeting_id=meeting.id).all()
        if feedback_rating(row) is not None
    }
    should_purge = should_purge or (bool(member_ids) and member_ids.issubset(rated_ids))
    should_purge = should_purge or chat_expired(meeting)
    if should_purge:
        ChatMessage.query.filter_by(meeting_id=meeting.id).delete(synchronize_session=False)
    return should_purge


def close_member_presences(meeting, stale_before=None):
    """A closed meeting must not leave either participant publicly open."""
    member_ids = accepted_user_ids(meeting)
    if not member_ids:
        return 0
    query = Presence.query.filter(Presence.user_id.in_(member_ids))
    if stale_before is not None:
        query = query.filter(Presence.updated_at <= stale_before)
    return query.delete(synchronize_session=False)


def purge_meeting_notifications(meeting, user_ids=None):
    """Remove legacy in-app chat/meeting notices without deleting unrelated reminders."""
    member_ids = accepted_user_ids(meeting)
    target_ids = set(user_ids or member_ids)
    if not target_ids:
        return 0
    member_names = [
        user.name for user_id in member_ids
        if (user := db.session.get(User, user_id)) is not None
    ]
    notices = UserNotification.query.filter(
        UserNotification.user_id.in_(target_ids),
        UserNotification.created_at >= meeting.created_at,
        UserNotification.kind != "presence_reminder",
        UserNotification.kind != "thanks",
    ).all()
    removed = 0
    for notice in notices:
        related = (
            meeting.description in notice.text
            or notice.kind.startswith("meeting_")
            or any(notice.text.startswith(f"{name}:") for name in member_names)
            or any(notice.text.startswith(f"{name} ") for name in member_names)
        )
        if related:
            db.session.delete(notice)
            removed += 1
    return removed


def closed_at_for(meeting, user_id):
    feedback = MeetingFeedback.query.filter_by(
        meeting_id=meeting.id, user_id=user_id
    ).first()
    if feedback:
        return normalize_dt(feedback.created_at)
    report = UserReport.query.filter(
        UserReport.meeting_id == meeting.id,
        UserReport.reporter_id == user_id,
        UserReport.category != "no_show_dispute",
    ).first()
    if report:
        return normalize_dt(report.created_at)
    leave = MeetingEvent.query.filter_by(
        meeting_id=meeting.id, user_id=user_id, kind="leave"
    ).first()
    if leave:
        return normalize_dt(leave.created_at)
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if state and state.status == "cancelled":
        return normalize_dt(state.updated_at)
    if chat_expired(meeting):
        return chat_cleanup_deadline(meeting)
    return None


def reconcile_closed_meetings(user):
    """Close stale accepted meetings and clean records when the user next opens the app."""
    meeting_ids = {
        row.meeting_id for row in Interest.query.filter_by(user_id=user.id).all()
    }
    meeting_ids.update(
        row.id for row in Meeting.query.filter_by(owner_id=user.id).all()
    )
    changed = False
    for meeting_id in meeting_ids:
        meeting = db.session.get(Meeting, meeting_id)
        if not meeting:
            continue
        has_accepted = Interest.query.filter_by(
            meeting_id=meeting.id, status="accepted"
        ).first() is not None
        stale_deadline = normalize_dt(meeting.starts_at) + timedelta(hours=24)
        state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
        raw_status = state.status if state else "active"
        if has_accepted and raw_status == "active" and stale_deadline <= utcnow():
            if not state:
                state = MeetingState(meeting_id=meeting.id, status="cancelled")
                db.session.add(state)
            else:
                state.status = "cancelled"
            meeting.expires_at = utcnow()
            changed = bool(close_member_presences(
                meeting, stale_before=stale_deadline
            )) or True
        if not chat_closed_for(meeting, user.id):
            continue
        closed_at = closed_at_for(meeting, user.id)
        if closed_at:
            changed = bool(close_member_presences(
                meeting, stale_before=closed_at
            )) or changed
        changed = bool(purge_meeting_notifications(meeting, {user.id})) or changed
        changed = purge_chat_if_ready(meeting) or changed
    if changed:
        db.session.commit()


def effective_meeting_status(meeting):
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if not state:
        return "active"
    if state.status == "completed" and not completed_by_all(meeting):
        return "active"
    return state.status


def trust_payload(user_id):
    completed_states = MeetingState.query.filter_by(status="completed").all()
    completed_ids = {
        state.meeting_id for state in completed_states
        if (meeting := db.session.get(Meeting, state.meeting_id)) and completed_by_all(meeting)
    }
    completed = 0
    for meeting_id in completed_ids:
        meeting = db.session.get(Meeting, meeting_id)
        if not meeting:
            continue
        if meeting.owner_id == user_id or Interest.query.filter_by(
                meeting_id=meeting_id, user_id=user_id, status="accepted").first():
            completed += 1
    thanks = MeetingThanks.query.filter_by(receiver_id=user_id).count()
    no_show_events = MeetingEvent.query.filter_by(
        target_user_id=user_id, kind="no_show"
    ).all()
    dispute_window = utcnow() - timedelta(hours=24)
    no_shows = 0
    for event in no_show_events:
        disputed = MeetingEvent.query.filter_by(
            meeting_id=event.meeting_id,
            user_id=user_id,
            target_user_id=event.user_id,
            kind="no_show_disputed",
        ).first()
        if disputed:
            dispute_report = UserReport.query.filter_by(
                meeting_id=event.meeting_id,
                reporter_id=user_id,
                target_id=event.user_id,
                category="no_show_dispute",
            ).first()
            if dispute_report and dispute_report.status == "dismissed":
                disputed = None
        if not disputed and normalize_dt(event.created_at) <= dispute_window:
            no_shows += 1

    received_ratings = []
    for feedback in MeetingFeedback.query.all():
        rating = feedback_rating(feedback)
        meeting = db.session.get(Meeting, feedback.meeting_id)
        if rating is None or not meeting or meeting.format != "one":
            continue
        member_ids = accepted_user_ids(meeting)
        if len(member_ids) == 2 and user_id in member_ids and feedback.user_id != user_id:
            received_ratings.append(rating)
    rating_count = len(received_ratings)
    average_rating = (
        round(sum(received_ratings) / rating_count, 1)
        if rating_count >= 3 else None
    )
    rating_adjustment = round((average_rating - 3) * 5) if average_rating is not None else 0
    score = max(20, min(
        100,
        70 + min(completed * 4, 20) + min(thanks * 3, 15)
        + rating_adjustment - min(no_shows * 15, 45),
    ))
    if completed < 3 and no_shows == 0:
        level = "Новый участник"
    elif completed >= 8 and thanks >= 3 and no_shows == 0:
        level = "Высокое доверие"
    elif completed >= 3 and no_shows == 0:
        level = "Надёжный участник"
    else:
        level = "Есть замечания"
    develops_club = Invitation.query.filter_by(inviter_id=user_id, status="rewarded").count() > 0
    return {
        "score": score,
        "level": level,
        "completed_meetings": completed,
        "thanks": thanks,
        "no_shows": no_shows,
        "average_rating": average_rating,
        "rating_count": rating_count,
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
    a = min(1.0, max(0.0, a))
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def approximate_zone(latitude, longitude):
    return round(round(latitude / 0.01) * 0.01, 2), round(round(longitude / 0.015) * 0.015, 3)


def distance_band(distance):
    for limit, label in ((1, "до 1 км"), (3, "1–3 км"), (5, "3–5 км"),
                         (10, "5–10 км"), (12, "10–12 км")):
        if distance <= limit:
            return limit, label
    return 12, "более 12 км"


def media_payload(media):
    return decrypt_selfie(media.public_image) if media and media.public_image else None


def valid_photo(value):
    if not isinstance(value, str) or len(value) > 700_000:
        return False
    header, _, payload = value.partition(",")
    signatures = {
        "data:image/jpeg;base64": b"\xff\xd8\xff",
        "data:image/png;base64": b"\x89PNG\r\n\x1a\n",
        "data:image/webp;base64": b"RIFF",
    }
    if header not in signatures:
        return False
    try:
        decoded = base64.b64decode(payload, validate=True)
    except ValueError:
        return False
    return decoded.startswith(signatures[header]) and (
        header != "data:image/webp;base64" or decoded[8:12] == b"WEBP"
    )


def notify_user(user_id, text, kind="info", dedupe_key=None):
    persistent = kind in {"presence_reminder", "meeting_confirmation"}
    if persistent:
        if dedupe_key and UserNotification.query.filter_by(dedupe_key=dedupe_key).first():
            return False
        db.session.add(UserNotification(
            user_id=user_id, kind=kind, text=str(text)[:300], dedupe_key=dedupe_key,
        ))
        bump_sync(user_ids={user_id}, notification_ids={user_id})
    if BOT_TOKEN:
        queue_user_telegram(user_id, kind, dedupe_key)
    db.session.commit()
    return True


def process_presence_reminders():
    threshold = utcnow() - timedelta(hours=1)
    active = Presence.query.filter(Presence.active_until > utcnow(), Presence.updated_at <= threshold).all()
    for presence in active:
        notify_user(
            presence.user_id,
            "Вы всё ещё открыты для общения. Оставить статус включённым или выключить его в приложении?",
            kind="presence_reminder",
            dedupe_key=presence_reminder_key(presence),
        )


def presence_reminder_loop():
    while True:
        time.sleep(60)
        try:
            with app.app_context():
                process_presence_reminders()
                process_company_watches()
                expire_task_materials()
        except Exception:
            record_runtime_error("background_errors")
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
        app.logger.warning("Telegram bot setup failed")


@app.before_request
def assign_request_id():
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    g.request_id = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{8,80}", supplied) else uuid.uuid4().hex
    g.request_started_at = time.perf_counter()


PUBLIC_RATE_LIMITS = {"/auth/handoff": 8, "/auth/telegram-mini-app": 20,
                      "/auth/telegram/start": 20, "/auth/device": 40, "/api/traffic": 120}
PUBLIC_RATE_BUCKETS = OrderedDict()
PUBLIC_RATE_LOCK = threading.Lock()
RUNTIME_ERRORS = {"http_5xx": 0, "background_errors": 0}
RUNTIME_ERRORS_LOCK = threading.Lock()


@app.before_request
def limit_public_requests():
    limit = PUBLIC_RATE_LIMITS.get(request.path)
    if not limit or ALLOW_TEST_AUTH:
        return None
    now = time.monotonic()
    key = (request.remote_addr, request.path)
    with PUBLIC_RATE_LOCK:
        bucket = PUBLIC_RATE_BUCKETS.pop(key, None)
        start, count = bucket if bucket and now - bucket[0] < 60 else (now, 0)
        PUBLIC_RATE_BUCKETS[key] = (start, count + 1)
        while len(PUBLIC_RATE_BUCKETS) > 10000:
            PUBLIC_RATE_BUCKETS.popitem(last=False)
    if count >= limit:
        return jsonify(error="Слишком много запросов. Повторите позже"), 429, {"Retry-After": "60"}
    return None


@app.before_request
def reject_cross_site_mutations():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("Sec-Fetch-Site") == "cross-site":
        if request.path.startswith("/api/"):
            return jsonify(error="Запрос из другого сайта отклонён"), 403


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


def record_runtime_error(kind):
    with RUNTIME_ERRORS_LOCK:
        RUNTIME_ERRORS[kind] += 1


@app.after_request
def security_headers(response):
    if response.status_code >= 500:
        record_runtime_error("http_5xx")
    request_id = getattr(g, "request_id", uuid.uuid4().hex)
    duration_ms = round((time.perf_counter() - getattr(g, "request_started_at", time.perf_counter())) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.path not in {"/about", "/sitemap.xml", "/robots.txt"}:
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "script-src 'self' 'unsafe-inline' https://telegram.org https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://*.tile.openstreetmap.org; "
        "connect-src 'self' https://nominatim.openstreetmap.org;"
    )
    response.headers["Permissions-Policy"] = "camera=(self), geolocation=(self), microphone=()"
    if PUBLIC_URL.startswith("https://"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    app.logger.info(
        "request_complete",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.get("/health")
def health():
    db.session.execute(db.select(db.literal(1)))
    return jsonify(
        ok=True,
        version=APP_VERSION,
        telegram_bot_configured=telegram_bot_configured,
    )


@app.get("/api/version")
def api_version():
    return jsonify(version=APP_VERSION)


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
        sse_enabled=ENABLE_SSE,
        is_admin=bool(user and user.telegram_id in ADMIN_TELEGRAM_IDS),
        profile_completed=bool(profile and selfie),
        private_tasks_enabled=bool(private_task_access()),
        user={
            "id": user.id,
            "name": user.name,
            "username": user.username,
            "picture": user.picture,
            "trust": trust_payload(user.id),
        } if user else None,
    )


@app.get("/api/sync-state")
@login_required
def sync_state():
    user = current_user()
    room_id = request.args.get("room_id", type=int)
    room_revision = 0
    if room_id:
        meeting = db.session.get(Meeting, room_id)
        if not meeting or not meeting_member(meeting, user):
            return jsonify(error="Нет доступа к встрече"), 403
        room_revision = revision_value(f"room:{room_id}")
    return jsonify(
        feed=revision_value("feed"),
        user=revision_value(f"user:{user.id}"),
        notifications=revision_value(f"notifications:{user.id}"),
        room=room_revision,
        room_id=room_id,
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
    record_acquisition(current_user(), source, medium, campaign)
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
        TrafficVisit.medium == medium,
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


class ProfileMedia(db.Model):
    __tablename__ = "profile_media_v2"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    public_kind = db.Column(db.String(20), nullable=False, default="neutral")
    public_image = db.Column(db.Text)
    real_photo = db.Column(db.Text)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class MeetingConfirmation(db.Model):
    __tablename__ = "meeting_confirmation_v2"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_meeting_confirmation_v2"),)


class GoodDeed(db.Model):
    __tablename__ = "good_deed_v2"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    direction = db.Column(db.String(40), nullable=False, default="community")
    description = db.Column(db.String(500), nullable=False)
    organizer_name = db.Column(db.String(120), nullable=False)
    coordinator_name = db.Column(db.String(120), nullable=False)
    area = db.Column(db.String(120), nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, nullable=False, default=120)
    capacity = db.Column(db.Integer, nullable=False, default=10)
    instructions = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    result_summary = db.Column(db.String(500))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class GoodDeedParticipant(db.Model):
    __tablename__ = "good_deed_participant_v2"
    id = db.Column(db.Integer, primary_key=True)
    deed_id = db.Column(db.Integer, db.ForeignKey("good_deed_v2.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="joined", index=True)
    hours = db.Column(db.Float, nullable=False, default=0)
    thanks = db.Column(db.String(180))
    joined_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    confirmed_at = db.Column(db.DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("deed_id", "user_id", name="uq_good_deed_participant_v2"),)


def user_profile_payload(user):
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    selfie = ProfileSelfie.query.filter_by(user_id=user.id).first()
    media = ProfileMedia.query.filter_by(user_id=user.id).first()
    return {
        "completed": bool(profile and selfie), "name": user.name,
        "age": profile.age if profile else None,
        "gender": profile.gender if profile else None,
        "city": profile.city if profile else "Минск",
        "about": selfie.about if selfie else "",
        "selfie_present": bool(selfie),
        "selfie_preview": decrypt_selfie(selfie.image) if selfie else None,
        "selfie_visibility": "hidden",
        "verification_label": "Селфи добавлено" if selfie else None,
        "public_image_kind": media.public_kind if media else "neutral",
        "public_image_preview": media_payload(media),
        "real_photo_present": bool(media and media.real_photo),
        "real_photo_preview": decrypt_selfie(media.real_photo) if media and media.real_photo else None,
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
    city = str(data.get("city") or "").strip()[:80]
    try:
        age = int(data.get("age"))
    except (TypeError, ValueError):
        return jsonify(error="Укажите возраст"), 400
    gender = str(data.get("gender", ""))
    about = str(data.get("about", "")).strip()[:160]
    selfie_image = str(data.get("selfie", ""))
    public_kind = data.get("public_image_kind")
    public_image = data.get("public_image")
    real_photo = data.get("real_photo")
    if len(name) < 2:
        return jsonify(error="Укажите имя — не короче двух букв"), 400
    if not 18 <= age <= 100:
        return jsonify(error="Приложение доступно пользователям от 18 до 100 лет"), 400
    if gender not in {"male", "female"}:
        return jsonify(error="Выберите вариант в поле «Пол»"), 400
    if len(about) < 20:
        return jsonify(error="Напишите одну короткую фразу о себе — минимум 20 символов"), 400
    if public_kind not in (None, "neutral", "avatar", "real"):
        return jsonify(error="Выберите тип публичного изображения"), 400
    if any(value and not valid_photo(value) for value in (public_image, real_photo)):
        return jsonify(error="Выберите корректное изображение"), 400
    if data.get("terms_accepted") is not True:
        return jsonify(error="Подтвердите правила безопасных встреч"), 400
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    selfie = ProfileSelfie.query.filter_by(user_id=user.id).first()
    if selfie_image:
        if not selfie_image.startswith("data:image/jpeg;base64,") or len(selfie_image) > 700_000:
            return jsonify(error="Не удалось обработать селфи. Сделайте новое фото"), 400
    elif not selfie:
        return jsonify(error="Добавьте свежее селфи"), 400
    media = ProfileMedia.query.filter_by(user_id=user.id).first()
    if public_kind in ("avatar", "real") and not public_image and not (media and media.public_image):
        return jsonify(error="Добавьте публичное изображение"), 400
    if public_kind is not None or real_photo is not None:
        if not media:
            media = ProfileMedia(user_id=user.id)
            db.session.add(media)
        if public_kind:
            media.public_kind = public_kind
        if public_kind == "neutral":
            media.public_image = None
        elif public_image:
            media.public_image = encrypt_selfie(public_image)
        if real_photo is not None:
            previous = decrypt_selfie(media.real_photo) if media.real_photo else None
            if real_photo != previous:
                media.real_photo = encrypt_selfie(real_photo) if real_photo else None
                meeting_ids = [row.meeting_id for row in PhotoConsent.query.filter_by(user_id=user.id)]
                if meeting_ids:
                    PhotoConsent.query.filter(PhotoConsent.meeting_id.in_(meeting_ids)).delete(synchronize_session=False)
                    for meeting in Meeting.query.filter(Meeting.id.in_(meeting_ids)):
                        bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
        bump_sync(feed=True)
    if not profile:
        profile = UserProfile(user_id=user.id, age=age, gender=gender, city=city or "Минск")
        db.session.add(profile)
    else:
        profile.age, profile.gender = age, gender
        if city:
            profile.city = city
        profile.terms_accepted_at = utcnow()
    if not selfie:
        selfie = ProfileSelfie(user_id=user.id, image=encrypt_selfie(selfie_image),
                               visibility="hidden", about=about)
        db.session.add(selfie)
    else:
        if selfie_image:
            selfie.image = encrypt_selfie(selfie_image)
        selfie.visibility, selfie.about = "hidden", about
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
                    app.logger.warning("Telegram code login failed")
                return jsonify(ok=True)
            try:
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Код не найден или его время истекло. Получите новый код в приложении «Сейчас».",
                })
            except requests.RequestException:
                app.logger.warning("Telegram expired code reply failed")
            return jsonify(ok=True)
        start_payload = ""
        if message_text.startswith("/start "):
            start_payload = message_text.split(maxsplit=1)[1][:80]
        web_app_url = PUBLIC_URL
        if start_payload.startswith(("login_", "invite_")):
            web_app_url = f"{PUBLIC_URL}/?handoff={quote(start_payload, safe='')}"
        try:
            if message_text.startswith("/start"):
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Откройте приложение «Сейчас» кнопкой ниже.",
                    "reply_markup": {"inline_keyboard": [[{
                        "text": "Открыть приложение", "web_app": {"url": web_app_url},
                    }]]},
                })
            else:
                sender = message.get("from") or {}
                user = upsert_telegram_user(sender)
                if consume_action(user.id, "bot_text_reply", 1, 60):
                    db.session.commit()
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": (
                            "Бот не принимает сообщения. Для общения используйте приложение, "
                            "для жалобы — кнопку «Пожаловаться» внутри встречи или профиля."
                        ),
                    })
                else:
                    db.session.rollback()
        except requests.RequestException:
            app.logger.warning("Telegram webhook reply failed")
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
    transaction_lock(User.query.filter_by(id=user_id), User.id)
    delete_private_features(user_id)
    ProfileMedia.query.filter_by(user_id=user_id).delete()
    MeetingConfirmation.query.filter_by(user_id=user_id).delete()
    GoodDeedParticipant.query.filter_by(user_id=user_id).delete()
    for deed in GoodDeed.query.filter_by(owner_id=user_id):
        GoodDeedParticipant.query.filter_by(deed_id=deed.id).delete()
        db.session.delete(deed)
    OperationRecord.query.filter_by(user_id=user_id).delete()
    ModerationAction.query.filter_by(moderator_id=user_id).delete()
    UserReport.query.filter_by(moderator_id=user_id).update({"moderator_id": None})
    proposed_place_ids = [row[0] for row in db.session.query(MeetingPlace.id).filter_by(user_id=user_id).all()]
    if proposed_place_ids:
        PlaceVote.query.filter(PlaceVote.place_id.in_(proposed_place_ids)).delete(synchronize_session=False)
        MeetingPlaceLocation.query.filter(
            MeetingPlaceLocation.place_id.in_(proposed_place_ids)).delete(synchronize_session=False)
    owned_meeting_ids = [row[0] for row in db.session.query(Meeting.id).filter_by(owner_id=user_id).all()]
    removed_reports = UserReport.query.filter(
        (UserReport.reporter_id == user_id) | (UserReport.target_id == user_id)
        | UserReport.meeting_id.in_(owned_meeting_ids)
    )
    report_ids = [report.id for report in removed_reports]
    if report_ids:
        ModerationAction.query.filter(ModerationAction.report_id.in_(report_ids)).delete(synchronize_session=False)
    if owned_meeting_ids:
        MeetingConfirmation.query.filter(MeetingConfirmation.meeting_id.in_(owned_meeting_ids)).delete(synchronize_session=False)
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
    if not user:
        return jsonify(items=[], agreed_places=[], guest=True)
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = min(max(request.args.get("radius", 3, type=float), 1), 12)
    category = request.args.get("category", "")
    time_mode = request.args.get("time", "now")
    zone = submitted_time_zone(request.args.get("time_zone")) or APP_TIMEZONE
    now = utcnow()
    result = []
    agreed_places = []
    blocked_ids = set()
    if user:
        reconcile_closed_meetings(user)
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
            if time_mode in {"hour", "today"}:
                continue
            if user and presence.user_id == user.id:
                continue
            if presence.user_id in blocked_ids:
                continue
            if user_hidden(presence.user_id):
                continue
            if category in VALID_CATEGORIES and presence.category != category:
                continue
            point = approximate_zone(presence.latitude, presence.longitude)
            distance = haversine_km(lat, lon, *point)
            upper, band = distance_band(distance)
            if distance > radius:
                continue
            profile = UserProfile.query.filter_by(user_id=presence.user_id).first()
            selfie = ProfileSelfie.query.filter_by(user_id=presence.user_id).first()
            trust = trust_payload(presence.user_id)
            media = ProfileMedia.query.filter_by(user_id=presence.user_id).first()
            result.append({
                "kind": "person", "id": presence.id, "icon": CATEGORY_ICONS[presence.category],
                "_owner_id": presence.user_id,
                "name": presence.user.name if user else "Участник рядом", "category": presence.category,
                "description": "Открыт к общению", "distance_km": upper, "distance_band": band, "zone_radius_m": 1050,
                "latitude": point[0], "longitude": point[1], "expires_at": normalize_dt(presence.active_until).isoformat(),
                "age": profile.age if profile else None, "gender": profile.gender if profile else None,
                "about": selfie.about if selfie else "", "profile_verified": bool(profile and selfie),
                "interested": presence.user_id in proposed_owner_ids,
                "trust": trust, "public_image": media_payload(media),
            })

        meetings = Meeting.query.filter(Meeting.expires_at > now).order_by(Meeting.id.desc()).all()
        interested_ids = set()
        if user:
            interested_ids = {i.meeting_id for i in Interest.query.filter_by(user_id=user.id).all()}
        for meeting in meetings:
            if targeted_meeting(meeting):
                continue
            if user and meeting.owner_id == user.id:
                continue
            if effective_meeting_status(meeting) != "active":
                continue
            accepted_count = Interest.query.filter_by(
                meeting_id=meeting.id, status="accepted"
            ).count()
            # A confirmed one-to-one meeting has moved into its private room and
            # must no longer remain as a public marker for either participant.
            if meeting.format == "one" and accepted_count:
                continue
            # A participant who has already closed this meeting must never see
            # the old public marker again, even while another participant is
            # still deciding how to finish it.
            if user and (
                chat_closed_for(meeting, user.id)
                or MeetingEvent.query.filter_by(
                    meeting_id=meeting.id, user_id=user.id, kind="complete"
                ).first()
            ):
                continue
            starts_at = normalize_dt(meeting.starts_at)
            is_now = starts_at <= now + timedelta(minutes=5)
            if time_mode == "now" and not is_now:
                continue
            if time_mode == "hour" and (is_now or starts_at > now + timedelta(minutes=60)):
                continue
            if time_mode == "today" and not now + timedelta(minutes=60) < starts_at < end_of_app_day(now, zone):
                continue
            if meeting.category == "help" and meeting.description not in SAFE_HELP_GOALS and (
                meeting.description != "Предложение встретиться · Помочь друг другу"
            ):
                continue
            if meeting.owner_id in blocked_ids:
                continue
            if user_hidden(meeting.owner_id):
                continue
            if category in VALID_CATEGORIES and meeting.category != category:
                continue
            point = approximate_zone(meeting.latitude, meeting.longitude)
            distance = haversine_km(lat, lon, *point)
            upper, band = distance_band(distance)
            if distance > radius:
                continue
            profile = UserProfile.query.filter_by(user_id=meeting.owner_id).first()
            selfie = ProfileSelfie.query.filter_by(user_id=meeting.owner_id).first()
            trust = trust_payload(meeting.owner_id)
            media = ProfileMedia.query.filter_by(user_id=meeting.owner_id).first()
            result.append({
                "kind": "meeting", "id": meeting.id, "icon": CATEGORY_ICONS[meeting.category],
                "_owner_id": meeting.owner_id,
                "name": meeting.owner.name if user else "Открытая встреча", "category": meeting.category,
                "description": meeting.description, "format": meeting.format, "distance_km": upper, "distance_band": band, "zone_radius_m": 1050,
                "latitude": point[0], "longitude": point[1], "mine": bool(user and meeting.owner_id == user.id),
                "interested": meeting.id in interested_ids, "expires_at": normalize_dt(meeting.expires_at).isoformat(),
                "starts_at": starts_at.isoformat(), "time_mode": "now" if is_now else ("hour" if starts_at <= now + timedelta(hours=1) else "today"),
                "starts_in_minutes": max(0, round((starts_at - now).total_seconds() / 60)),
                "age": profile.age if profile else None, "gender": profile.gender if profile else None,
                "about": selfie.about if selfie else "", "profile_verified": bool(profile and selfie),
                "trust": trust, "public_image": media_payload(media),
            })
    if user:
        for meeting in Meeting.query.filter(Meeting.expires_at > now).all():
            if not meeting_member(meeting, user):
                continue
            state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
            if state and state.status != "active":
                continue
            confirmed = MeetingPlace.query.filter_by(
                meeting_id=meeting.id, confirmed=1
            ).order_by(MeetingPlace.id.desc()).first()
            if not confirmed or not agreed_public_place(meeting, confirmed):
                continue
            location = MeetingPlaceLocation.query.filter_by(place_id=confirmed.id).first()
            if not location:
                continue
            agreed_places.append({
                "meeting_id": meeting.id,
                "title": confirmed.title,
                "description": meeting.description,
                "latitude": location.latitude,
                "longitude": location.longitude,
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
    return jsonify(items=result, agreed_places=agreed_places)


@app.post("/api/location")
@login_required
def location():
    user = current_user()
    data = json_body()
    if not valid_coordinates(data.get("latitude"), data.get("longitude")):
        return jsonify(error="Некорректная геолокация"), 400
    user.latitude, user.longitude = float(data["latitude"]), float(data["longitude"])
    bump_sync(feed=True, user_ids={user.id})
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/presence")
@login_required
@profile_required
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
    bump_sync(feed=True, user_ids={user.id})
    db.session.commit()
    return jsonify(ok=True, active_until=None,
                   reminder_due_at=(normalize_dt(presence.updated_at) + timedelta(hours=1)).isoformat())


@app.delete("/api/presence")
@login_required
def stop_presence():
    user = current_user()
    Presence.query.filter_by(user_id=user.id).delete()
    bump_sync(feed=True, user_ids={user.id})
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/presence")
@login_required
def get_presence():
    user = current_user()
    presence = Presence.query.filter_by(user_id=user.id).first()
    if presence and normalize_dt(presence.active_until) > utcnow():
        accepted_rows = []
        for interest in Interest.query.filter_by(status="accepted").all():
            meeting = db.session.get(Meeting, interest.meeting_id)
            if meeting and effective_meeting_status(meeting) == "active" and (
                meeting.owner_id == user.id or interest.user_id == user.id
            ):
                accepted_rows.append(interest)
        if accepted_rows and normalize_dt(presence.updated_at) <= max(
            normalize_dt(row.created_at) for row in accepted_rows
        ):
            presence.active_until = utcnow()
            db.session.commit()
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
    reconcile_closed_meetings(user)
    now = utcnow()
    presence_active = Presence.query.filter(
        Presence.user_id == user.id, Presence.active_until > now
    ).first() is not None
    items = UserNotification.query.filter_by(user_id=user.id).order_by(
        UserNotification.id.desc()).all()
    active_items = []
    for item in items:
        keep = item.kind == "presence_reminder" and presence_active
        keep = keep or item.kind == "moderation_warning"
        if item.kind == "meeting_confirmation" and item.dedupe_key:
            try:
                meeting_id = int(item.dedupe_key.split(":")[1])
            except (IndexError, ValueError):
                meeting_id = 0
            meeting = db.session.get(Meeting, meeting_id)
            keep = bool(
                meeting
                and meeting_member(meeting, user)
                and effective_meeting_status(meeting) == "active"
                and not MeetingEvent.query.filter_by(
                    meeting_id=meeting_id, user_id=user.id, kind="complete"
                ).first()
            )
        if keep:
            active_items.append(item)
        else:
            db.session.delete(item)
    db.session.commit()
    active_items = active_items[:10]
    unread_items = [item for item in active_items if item.read_at is None]
    return jsonify(
        unread=len(unread_items),
        items=[{
            "id": item.id,
            "kind": item.kind,
            "text": item.text,
            "read": item.read_at is not None,
            "created_at": normalize_dt(item.created_at).isoformat(),
        } for item in active_items],
    )


@app.post("/api/notifications/read")
@login_required
def read_notifications():
    user_id = current_user().id
    UserNotification.query.filter_by(user_id=user_id, read_at=None).update(
        {"read_at": utcnow()}, synchronize_session=False)
    bump_sync(user_ids={user_id}, notification_ids={user_id})
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/meetings")
@login_required
@profile_required
def create_meeting():
    user = current_user()
    data = json_body()
    operation_id = request_operation_id()
    if operation_id == "":
        return jsonify(error="Некорректный Operation ID", code="INVALID_OPERATION_ID"), 400
    if replay := replay_operation(user, operation_id):
        return replay
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
    if category == "help" and description not in SAFE_HELP_GOALS:
        return jsonify(error="Выберите дистанционную помощь или помощь в публичном месте"), 400
    if meeting_format not in {"one", "group"}:
        return jsonify(error="Некорректный формат встречи"), 400
    if time_mode not in {"now", "hour", "today"}:
        return jsonify(error="Некорректное время встречи"), 400
    zone = submitted_time_zone(data.get("time_zone"))
    if zone is None:
        return jsonify(error="Некорректный часовой пояс"), 400
    now = utcnow()
    starts_at = now + timedelta(minutes=starts_in_minutes) if 0 <= starts_in_minutes <= 1440 else None
    if not starts_at or (starts_in_minutes and starts_at >= end_of_app_day(now, zone)):
        return jsonify(error="Выберите время до конца текущего дня"), 400
    if time_mode == "hour" and starts_in_minutes not in {15, 30, 45, 60}:
        return jsonify(error="Выберите время в пределах часа"), 400
    if time_mode == "now" and starts_in_minutes != 0:
        return jsonify(error="Для встречи прямо сейчас время должно быть нулевым"), 400
    if time_mode != "now" and starts_in_minutes == 0:
        return jsonify(error="Выберите время встречи"), 400
    if not valid_coordinates(lat, lon):
        return jsonify(error="Разрешите геолокацию для создания встречи"), 400
    lock_user_actions({user.id})
    if replay := replay_operation(user, operation_id):
        return replay
    if active_confirmed_meeting_ids(user.id):
        return jsonify(
            error="Сначала завершите текущую подтверждённую встречу",
            code="ACTIVE_MEETING_CONFLICT",
        ), 409
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
    meeting = Meeting(
        owner_id=user.id, category=category, description=description, format=meeting_format,
        latitude=float(lat), longitude=float(lon), starts_at=starts_at,
        expires_at=starts_at + timedelta(minutes=60),
    )
    user.latitude, user.longitude = float(lat), float(lon)
    db.session.add(meeting)
    db.session.flush()
    payload = {"ok": True, "id": meeting.id}
    bump_sync(feed=True, user_ids={user.id}, meeting_ids={meeting.id})
    remember_operation(user, operation_id, payload, 201)
    db.session.commit()
    return jsonify(payload), 201


def past_meeting_membership(user_id, old_meeting_id):
    previous = db.session.get(Meeting, old_meeting_id)
    if not previous or effective_meeting_status(previous) != "completed":
        return set()
    members = accepted_user_ids(previous)
    return members if user_id in members else set()


@app.get("/api/meetings/previous-contacts")
@login_required
def previous_meeting_contacts():
    user = current_user()
    participations = db.session.query(Interest.meeting_id).filter_by(user_id=user.id, status="accepted")
    meetings = Meeting.query.join(MeetingState).filter(
        MeetingState.status == "completed",
        (Meeting.owner_id == user.id) | Meeting.id.in_(participations)
    ).order_by(Meeting.starts_at.desc()).limit(30)
    contacts = {}
    for meeting in meetings:
        for peer_id in past_meeting_membership(user.id, meeting.id) - {user.id}:
            if peer_id not in contacts and meeting_peer_available(user.id, peer_id):
                peer = db.session.get(User, peer_id)
                if peer:
                    contacts[peer_id] = {"user_id": peer.id, "name": peer.name, "meeting_id": meeting.id}
    return jsonify(items=list(contacts.values()))


def meeting_peer_available(user_id, peer_id):
    return not user_hidden(peer_id) and not UserBlock.query.filter(
        ((UserBlock.blocker_id == user_id) & (UserBlock.blocked_id == peer_id))
        | ((UserBlock.blocker_id == peer_id) & (UserBlock.blocked_id == user_id))
    ).first()


@app.post("/api/meetings/<int:meeting_id>/repeat")
@login_required
@profile_required
def repeat_meeting(meeting_id):
    user = current_user()
    peer_id = json_body().get("user_id")
    if type(peer_id) is not int or peer_id == user.id or peer_id not in past_meeting_membership(user.id, meeting_id):
        abort(404)
    if not meeting_peer_available(user.id, peer_id):
        return jsonify(error="Повторное приглашение недоступно"), 403
    transaction_lock(User.query.filter_by(id=user.id), User.id)
    label = "Повторная встреча · предложение"
    existing = Interest.query.join(Meeting).filter(
        Interest.user_id == user.id, Meeting.owner_id == peer_id,
        Meeting.description == label, Meeting.expires_at > utcnow()
    ).first()
    if existing:
        return jsonify(ok=True, meeting_id=existing.meeting_id)
    if not consume_action(user.id, "repeat_meeting", 3, 3600):
        return jsonify(error="Слишком много приглашений за час"), 429
    now = utcnow()
    meeting = Meeting(owner_id=peer_id, category="talk", description=label, format="one",
                      latitude=0, longitude=0, starts_at=now, expires_at=now+timedelta(hours=1))
    db.session.add(meeting)
    db.session.flush()
    db.session.add(Interest(meeting_id=meeting.id, user_id=user.id))
    bump_sync(user_ids={peer_id, user.id}, meeting_ids={meeting.id}, notification_ids={peer_id})
    db.session.commit()
    emit_realtime({peer_id, user.id}, "interest", meeting.id)
    notify_user(peer_id, "Вас приглашают на повторную встречу", kind="repeat_meeting",
                dedupe_key=f"repeat:{meeting.id}")
    return jsonify(ok=True, meeting_id=meeting.id), 201


def targeted_meeting(meeting):
    return meeting.description.startswith(("Предложение встретиться · ", "Повторная встреча · "))


@app.post("/api/presences/<int:presence_id>/interest")
@login_required
@profile_required
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
    bump_sync(user_ids={presence.user_id, user.id}, meeting_ids={meeting.id},
              notification_ids={presence.user_id})
    db.session.commit()
    emit_realtime({presence.user_id, user.id}, "interest", meeting.id)
    notify_user(
        presence.user_id,
        f"{user.name} предлагает встретиться: {CATEGORY_MEETING_TITLES[presence.category]}",
    )
    return jsonify(ok=True, meeting_id=meeting.id, already_sent=False), 201


@app.post("/api/meetings/<int:meeting_id>/interest")
@login_required
@profile_required
def express_interest(meeting_id):
    user = current_user()
    meeting = db.get_or_404(Meeting, meeting_id)
    if targeted_meeting(meeting):
        abort(404)
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
        bump_sync(user_ids={meeting.owner_id, user.id}, meeting_ids={meeting.id},
                  notification_ids={meeting.owner_id})
        db.session.commit()
        emit_realtime({meeting.owner_id, user.id}, "interest", meeting.id)
        notify_user(
            meeting.owner_id,
            f"Новый отклик на встречу «{meeting.description}» от {user.name}",
        )
    return jsonify(ok=True)


def interest_payload(interest, viewer):
    meeting = db.session.get(Meeting, interest.meeting_id)
    participant = db.session.get(User, interest.user_id)
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
        **meeting_list_summary(meeting, viewer),
    }


def meeting_list_summary(meeting, viewer):
    """Return one shared summary for every representation of the same meeting."""
    purge_chat_if_ready(meeting)
    member_ids = {meeting.owner_id}
    member_ids.update(row.user_id for row in Interest.query.filter_by(
        meeting_id=meeting.id, status="accepted"
    ).all())
    people = []
    for user_id in sorted(member_ids):
        if user_id == viewer.id:
            continue
        person = db.session.get(User, user_id)
        if person and person.name not in people:
            people.append(person.name)
    my_completion_confirmed = MeetingEvent.query.filter_by(
        meeting_id=meeting.id, user_id=viewer.id, kind="complete"
    ).first() is not None
    latest = None if my_completion_confirmed else (
        ChatMessage.query.filter_by(meeting_id=meeting.id)
        .order_by(ChatMessage.id.desc()).first()
    )
    confirmed_place = (MeetingPlace.query.filter_by(meeting_id=meeting.id, confirmed=1)
                       .order_by(MeetingPlace.id.desc()).first())
    my_feedback = MeetingFeedback.query.filter_by(
        meeting_id=meeting.id, user_id=viewer.id
    ).first()
    return {
        "people": people,
        "meeting_status": effective_meeting_status(meeting),
        "my_completion_confirmed": my_completion_confirmed,
        "needs_feedback": my_completion_confirmed and feedback_rating(my_feedback) is None,
        "confirmed_place": confirmed_place.title if confirmed_place else None,
        "latest_message": {
            "id": latest.id,
            "name": db.session.get(User, latest.user_id).name,
            "text": latest.text,
            "mine": latest.user_id == viewer.id,
            "created_at": normalize_dt(latest.created_at).isoformat(),
        } if latest else None,
    }


@app.get("/api/interests")
@login_required
def list_interests():
    user = current_user()
    reconcile_closed_meetings(user)
    now = utcnow()
    chat_cleanup_needed = False
    all_meetings = Meeting.query.order_by(Meeting.id.desc()).all()
    owned_meetings = []
    for meeting in all_meetings:
        chat_cleanup_needed = purge_chat_if_ready(meeting) or chat_cleanup_needed
        if meeting.owner_id != user.id or effective_meeting_status(meeting) == "cancelled":
            continue
        if chat_closed_for(meeting, user.id):
            continue
        has_accepted = Interest.query.filter_by(
            meeting_id=meeting.id, status="accepted"
        ).first() is not None
        if normalize_dt(meeting.expires_at) > now or has_accepted:
            owned_meetings.append(meeting)
    owned_meeting_ids = [row.id for row in owned_meetings]
    incoming = []
    if owned_meeting_ids:
        for interest in Interest.query.filter(Interest.meeting_id.in_(owned_meeting_ids)).all():
            meeting = db.session.get(Meeting, interest.meeting_id)
            chat_cleanup_needed = purge_chat_if_ready(meeting) or chat_cleanup_needed
            if not chat_closed_for(meeting, user.id) and (
                    interest.status == "accepted" or normalize_dt(meeting.expires_at) > now):
                incoming.append(interest)
    outgoing = []
    for interest in Interest.query.filter_by(user_id=user.id).all():
        meeting = db.session.get(Meeting, interest.meeting_id)
        if meeting:
            chat_cleanup_needed = purge_chat_if_ready(meeting) or chat_cleanup_needed
        if not meeting or effective_meeting_status(meeting) == "cancelled":
            continue
        if chat_closed_for(meeting, user.id):
            continue
        if interest.status == "accepted" or normalize_dt(meeting.expires_at) > now:
            outgoing.append(interest)
    if chat_cleanup_needed:
        db.session.commit()
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
            **meeting_list_summary(meeting, user),
        } for meeting in owned_meetings],
        incoming=[interest_payload(item, user) for item in incoming],
        outgoing=[interest_payload(item, user) for item in outgoing],
    )


@app.post("/api/interests/<int:interest_id>/decision")
@login_required
def decide_interest(interest_id):
    user = current_user()
    operation_id = request_operation_id()
    if operation_id == "":
        return jsonify(error="Некорректный Operation ID", code="INVALID_OPERATION_ID"), 400
    if replay := replay_operation(user, operation_id):
        return replay
    interest = Interest.query.filter_by(id=interest_id).with_for_update().first()
    if not interest:
        abort(404)
    meeting = Meeting.query.filter_by(id=interest.meeting_id).with_for_update().first()
    if meeting.owner_id != user.id:
        return jsonify(error="Решение принимает создатель встречи"), 403
    lock_user_actions({meeting.owner_id, interest.user_id})
    if replay := replay_operation(user, operation_id):
        return replay
    if interest.status != "pending":
        return jsonify(error="По этому отклику решение уже принято"), 409
    decision = json_body().get("decision")
    if decision not in {"accepted", "rejected"}:
        return jsonify(error="Выберите принять или отклонить"), 400
    if normalize_dt(meeting.expires_at) <= utcnow() or effective_meeting_status(meeting) != "active":
        return jsonify(error="Встреча уже недоступна", code="MEETING_NOT_ACTIVE"), 409
    if decision == "accepted":
        if not meeting_peer_available(meeting.owner_id, interest.user_id):
            return jsonify(error="Участие больше недоступно"), 403
        conflicting_users = [
            member_id for member_id in (meeting.owner_id, interest.user_id)
            if active_confirmed_meeting_ids(member_id, exclude_meeting_id=meeting.id)
        ]
        if conflicting_users:
            return jsonify(
                error="Участник уже находится в другой активной встрече",
                code="ACTIVE_MEETING_CONFLICT",
            ), 409
        accepted_count = Interest.query.filter_by(meeting_id=meeting.id, status="accepted").count()
        if meeting.format == "one" and accepted_count:
            return jsonify(error="Личная встреча уже занята", code="MEETING_CAPACITY_REACHED"), 409
        if meeting.format == "group" and accepted_count >= 5:
            return jsonify(error="Группа уже набрана", code="MEETING_CAPACITY_REACHED"), 409
    interest.status = decision
    if decision == "accepted" and meeting.format == "one":
        (Interest.query.filter_by(meeting_id=meeting.id, status="pending")
         .filter(Interest.id != interest.id).update({"status": "rejected"}, synchronize_session=False))
    if decision == "accepted":
        clear_meeting_consents(meeting.id)
        state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
        if not state:
            db.session.add(MeetingState(meeting_id=meeting.id, status="active"))
        member_ids = {meeting.owner_id, interest.user_id}
        Presence.query.filter(Presence.user_id.in_(member_ids)).update(
            {"active_until": utcnow()}, synchronize_session=False
        )
    db.session.flush()
    payload = {"ok": True, "interest": interest_payload(interest, user)}
    bump_sync(feed=decision == "accepted",
              user_ids={meeting.owner_id, interest.user_id}, meeting_ids={meeting.id},
              notification_ids={interest.user_id})
    remember_operation(user, operation_id, payload, 200)
    db.session.commit()
    emit_realtime({meeting.owner_id, interest.user_id}, f"interest_{decision}", meeting.id)
    result_text = "принят" if decision == "accepted" else "отклонён"
    notify_user(
        interest.user_id,
        f"Ваш отклик на встречу «{meeting.description}» {result_text}",
    )
    return jsonify(payload)


def meeting_member(meeting, user):
    if meeting.owner_id == user.id:
        return True
    return Interest.query.filter_by(meeting_id=meeting.id, user_id=user.id, status="accepted").first() is not None


def clear_meeting_consents(meeting_id):
    PhotoConsent.query.filter_by(meeting_id=meeting_id).delete()
    MeetingConfirmation.query.filter_by(meeting_id=meeting_id).delete()


def meeting_room_or_error(meeting_id):
    meeting = db.get_or_404(Meeting, meeting_id)
    if not meeting_member(meeting, current_user()):
        return None, (jsonify(error="Чат откроется после подтверждения участия"), 403)
    purge_chat_if_ready(meeting)
    if chat_closed_for(meeting, current_user().id):
        db.session.commit()
        return None, (jsonify(error="Эта встреча закрыта, переписка удалена"), 410)
    return meeting, None


def room_payload(meeting, user):
    places = MeetingPlace.query.filter_by(meeting_id=meeting.id).order_by(MeetingPlace.id).all()
    votes = PlaceVote.query.filter(PlaceVote.place_id.in_([p.id for p in places])).all() if places else []
    vote_counts, my_votes = {}, set()
    for vote in votes:
        vote_counts[vote.place_id] = vote_counts.get(vote.place_id, 0) + 1
        if vote.user_id == user.id:
            my_votes.add(vote.place_id)
    if meeting.format == "one" and len(accepted_user_ids(meeting)) == 2:
        agreed = next((place for place in places if vote_counts.get(place.id, 0) >= 2), None)
        if agreed and not agreed.confirmed:
            MeetingPlace.query.filter_by(meeting_id=meeting.id).update(
                {"confirmed": 0}, synchronize_session=False
            )
            agreed.confirmed = 1
            db.session.commit()
    my_completion_confirmed = MeetingEvent.query.filter_by(
        meeting_id=meeting.id, user_id=user.id, kind="complete"
    ).first() is not None
    messages = []
    if not my_completion_confirmed:
        messages = list(reversed(
            ChatMessage.query.filter_by(meeting_id=meeting.id)
            .order_by(ChatMessage.id.desc()).limit(100).all()
        ))
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    raw_events = MeetingEvent.query.filter_by(meeting_id=meeting.id).order_by(MeetingEvent.id.desc()).all()
    event_keys, events = set(), []
    for event in raw_events:
        key = (event.kind, event.user_id, event.target_user_id)
        if key not in event_keys:
            event_keys.add(key)
            events.append(event)
    events.reverse()
    thanks = MeetingThanks.query.filter_by(meeting_id=meeting.id).all()
    member_ids = accepted_user_ids(meeting)
    consented_ids = {row.user_id for row in PhotoConsent.query.filter_by(meeting_id=meeting.id).all()}
    media_by_user = {row.user_id: row for row in ProfileMedia.query.filter(ProfileMedia.user_id.in_(member_ids))}
    photos_revealed = len(member_ids) >= 2 and member_ids.issubset(consented_ids) and all(
        uid in media_by_user and media_by_user[uid].real_photo for uid in member_ids
    )
    confirmed_ids = {row.user_id for row in MeetingConfirmation.query.filter_by(meeting_id=meeting.id)}
    participant_payload = []
    for uid in member_ids:
        participant = db.session.get(User, uid)
        profile = UserProfile.query.filter_by(user_id=uid).first()
        selfie = ProfileSelfie.query.filter_by(user_id=uid).first()
        media = media_by_user.get(uid)
        picture = decrypt_selfie(media.real_photo) if media and media.real_photo and (
            uid == user.id or photos_revealed
        ) else None
        participant_payload.append({
            "id": uid, "name": participant.name, "mine": uid == user.id,
            "age": profile.age if profile else None,
            "gender": profile.gender if profile else None,
            "about": selfie.about if selfie else "",
            "picture": picture, "public_picture": media_payload(media),
            "photo_visible": bool(picture), "photo_visibility": "mutual",
            "selfie_verified": bool(selfie),
            "photo_consented": uid in consented_ids,
            "trust": trust_payload(uid),
            "thanked_by_me": any(row.giver_id == user.id and row.receiver_id == uid for row in thanks),
        })
    my_late = any(event.kind == "late" and event.user_id == user.id for event in events)
    no_show_against_me = next((
        event for event in raw_events
        if event.kind == "no_show" and event.target_user_id == user.id
    ), None)
    no_show_disputed = bool(no_show_against_me and MeetingEvent.query.filter_by(
        meeting_id=meeting.id,
        user_id=user.id,
        target_user_id=no_show_against_me.user_id,
        kind="no_show_disputed",
    ).first())
    completion_ids = {
        event.user_id for event in raw_events if event.kind == "complete"
    }
    status = effective_meeting_status(meeting)
    my_feedback = MeetingFeedback.query.filter_by(meeting_id=meeting.id, user_id=user.id).first()
    return {"meeting": {"id": meeting.id, "description": meeting.description, "format": meeting.format,
                         "latitude": None, "longitude": None,
                         "my_confirmed": user.id in confirmed_ids,
                         "all_confirmed": len(member_ids) >= 2 and member_ids.issubset(confirmed_ids),
                         "starts_at": normalize_dt(meeting.starts_at).isoformat(),
                         "is_owner": meeting.owner_id == user.id, "status": status,
                         "my_late": my_late,
                         "can_mark_no_show": utcnow() >= normalize_dt(meeting.starts_at),
                         "can_dispute_no_show": bool(no_show_against_me and not no_show_disputed),
                         "my_completion_confirmed": user.id in completion_ids,
                         "needs_feedback": user.id in completion_ids and feedback_rating(my_feedback) is None,
                         "completion_confirmed_count": len(completion_ids & member_ids),
                         "completion_required_count": len(member_ids)},
            "places": [{
                "id": p.id,
                "title": p.title,
                "votes": vote_counts.get(p.id, 0),
                "voted": p.id in my_votes,
                "confirmed": bool(p.confirmed),
                "latitude": location.latitude if (location := MeetingPlaceLocation.query.filter_by(
                    place_id=p.id).first() if p.confirmed and agreed_public_place(meeting, p) else None) else None,
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
                        "target_user_id": e.target_user_id,
                        "note": e.note or ""} for e in events],
            "traces": [],
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
    Meeting.query.filter_by(id=meeting.id).with_for_update().one()
    media = ProfileMedia.query.filter_by(user_id=user.id).first()
    if len(accepted_user_ids(meeting)) < 2 or not media or not media.real_photo:
        return jsonify(error="Добавьте отдельное фото и дождитесь принятия отклика"), 409
    consent = PhotoConsent.query.filter_by(meeting_id=meeting.id, user_id=user.id).first()
    if not consent:
        db.session.add(PhotoConsent(meeting_id=meeting.id, user_id=user.id))
        bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
        db.session.commit()
        emit_meeting(meeting, "photo_consent")
    return jsonify(ok=True, room=room_payload(meeting, user))


@app.post("/api/v2/meetings/<int:meeting_id>/confirm")
@login_required
def confirm_meeting(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    meeting = Meeting.query.filter_by(id=meeting_id).with_for_update().one()
    members = accepted_user_ids(meeting)
    if len(members) < 2 or effective_meeting_status(meeting) != "active":
        return jsonify(error="Нет активной встречи с подтверждённым участником"), 409
    user = current_user()
    if not MeetingConfirmation.query.filter_by(meeting_id=meeting.id, user_id=user.id).first():
        db.session.add(MeetingConfirmation(meeting_id=meeting.id, user_id=user.id))
        bump_sync(user_ids=members, meeting_ids={meeting.id})
        db.session.commit()
        emit_meeting(meeting)
    return jsonify(ok=True, room=room_payload(meeting, user))


def agreed_public_place(meeting, place):
    members = accepted_user_ids(meeting)
    votes = {row.user_id for row in PlaceVote.query.filter_by(place_id=place.id)}
    return len(members) >= 2 and members.issubset(votes) and place_is_public(place.title)


def place_is_public(title):
    private = r"квартир|подъезд|частн\w* дом|у (меня|тебя|него|неё) дома|мой дом"
    return not re.search(private, title.casefold())


@app.post("/api/meetings/<int:meeting_id>/places")
@login_required
def propose_place(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    title = str(json_body().get("title", "")).strip()[:120]
    if len(title) < 2:
        return jsonify(error="Укажите место встречи"), 400
    if not place_is_public(title):
        return jsonify(error="Выберите публичное место встречи"), 400
    latitude = json_body().get("latitude")
    longitude = json_body().get("longitude")
    has_coordinates = latitude is not None or longitude is not None
    if has_coordinates and not valid_coordinates(latitude, longitude):
        return jsonify(error="Некорректная точка на карте"), 400
    existing_place = None
    for candidate in MeetingPlace.query.filter_by(meeting_id=meeting.id).all():
        candidate_location = MeetingPlaceLocation.query.filter_by(place_id=candidate.id).first()
        same_title = candidate.title.strip().casefold() == title.casefold()
        same_point = bool(
            has_coordinates and candidate_location
            and haversine_km(
                float(latitude), float(longitude),
                candidate_location.latitude, candidate_location.longitude,
            ) <= 0.03
        )
        if same_title or same_point:
            existing_place = candidate
            break
    if existing_place:
        if not PlaceVote.query.filter_by(
            place_id=existing_place.id, user_id=current_user().id
        ).first():
            db.session.add(PlaceVote(
                place_id=existing_place.id, user_id=current_user().id
            ))
            bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
            db.session.commit()
            maybe_confirm_place(meeting, existing_place)
            emit_meeting(meeting, "place_vote")
        return jsonify(
            ok=True, already_exists=True,
            room=room_payload(meeting, current_user()),
        )

    place = MeetingPlace(meeting_id=meeting.id, user_id=current_user().id, title=title)
    db.session.add(place)
    db.session.flush()
    if has_coordinates:
        db.session.add(MeetingPlaceLocation(
            place_id=place.id, latitude=float(latitude), longitude=float(longitude), source="map",
        ))
    db.session.add(PlaceVote(place_id=place.id, user_id=current_user().id))
    bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
    db.session.commit()
    emit_meeting(meeting, "place_proposed")
    maybe_confirm_place(meeting, place)
    return jsonify(ok=True, room=room_payload(meeting, current_user())), 201


@app.post("/api/geocode/reverse")
@login_required
def reverse_geocode():
    global last_geocode_request_at
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
        with GEOCODE_LOCK:
            wait_seconds = 1.05 - (time.monotonic() - last_geocode_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            response = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": latitude, "lon": longitude, "format": "jsonv2", "accept-language": "ru"},
                headers={"User-Agent": f"Sichas-Minsk/0.15 ({PUBLIC_URL}; dudicoffnet@gmail.com)"},
                timeout=6,
            )
            last_geocode_request_at = time.monotonic()
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
        app.logger.warning("Reverse geocoding failed")
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
    if vote:
        if place.confirmed:
            return jsonify(error="Место уже согласовано всеми участниками"), 409
        db.session.delete(vote)
    else:
        db.session.add(PlaceVote(place_id=place.id, user_id=current_user().id))
    bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
    db.session.commit()
    if not vote:
        maybe_confirm_place(meeting, place)
    emit_meeting(meeting, "place_vote")
    return jsonify(ok=True, room=room_payload(meeting, current_user()))


def maybe_confirm_place(meeting, place, force=False):
    member_ids = accepted_user_ids(meeting)
    vote_count = PlaceVote.query.filter_by(place_id=place.id).count()
    should_confirm = force or (len(member_ids) >= 2 and vote_count >= len(member_ids))
    if not should_confirm or place.confirmed:
        return False
    MeetingPlace.query.filter_by(meeting_id=meeting.id).update(
        {"confirmed": 0}, synchronize_session=False
    )
    place.confirmed = 1
    bump_sync(user_ids=member_ids, meeting_ids={meeting.id}, feed=True)
    db.session.commit()
    emit_meeting(meeting, "place_confirmed")
    return True


@app.post("/api/places/<int:place_id>/confirm")
@login_required
def confirm_place(place_id):
    place = db.get_or_404(MeetingPlace, place_id)
    meeting = db.session.get(Meeting, place.meeting_id)
    if meeting.owner_id != current_user().id:
        return jsonify(error="Место подтверждает создатель встречи"), 403
    if MeetingPlaceLocation.query.filter_by(place_id=place.id).first() and not agreed_public_place(meeting, place):
        return jsonify(error="Точку на карте должны согласовать все участники"), 409
    maybe_confirm_place(meeting, place, force=True)
    emit_meeting(meeting, "place_confirmed")
    return jsonify(ok=True, room=room_payload(meeting, current_user()))


@app.post("/api/meetings/<int:meeting_id>/messages")
@login_required
@profile_required
def send_message(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    if len(accepted_user_ids(meeting)) < 2:
        return jsonify(error="Чат откроется после принятия отклика"), 409
    if effective_meeting_status(meeting) != "active" or MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=current_user().id, kind="complete"
    ).first():
        return jsonify(error="Эта переписка уже закрыта"), 409
    text = str(json_body().get("text", "")).strip()[:500]
    if not text:
        return jsonify(error="Напишите сообщение"), 400
    user = current_user()
    operation_id = request_operation_id()
    if operation_id == "":
        return jsonify(error="Некорректный Operation ID", code="INVALID_OPERATION_ID"), 400
    if replay := replay_operation(user, operation_id):
        return replay
    lock_user_actions({user.id})
    if replay := replay_operation(user, operation_id):
        return replay
    if not consume_action(user.id, "message", 12, 60):
        return jsonify(error="Слишком много сообщений. Подождите минуту"), 429
    message = ChatMessage(meeting_id=meeting.id, user_id=user.id, text=text)
    db.session.add(message)
    db.session.flush()
    payload = {"ok": True, "message_id": message.id, "room": room_payload(meeting, user)}
    bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
    remember_operation(user, operation_id, {"ok": True, "message_id": message.id, "_room_meeting_id": meeting.id}, 201)
    db.session.commit()
    emit_meeting(meeting, "chat_message")
    return jsonify(payload), 201


def state_for(meeting):
    state = MeetingState.query.filter_by(meeting_id=meeting.id).first()
    if not state:
        state = MeetingState(meeting_id=meeting.id, status="active")
        db.session.add(state)
    return state


def accepted_user_ids(meeting):
    return {meeting.owner_id} | {i.user_id for i in Interest.query.filter_by(
        meeting_id=meeting.id, status="accepted").all()}


def emit_meeting(meeting, kind="meeting_changed"):
    emit_realtime(accepted_user_ids(meeting), kind, meeting.id)


@app.post("/api/meetings/<int:meeting_id>/lifecycle")
@login_required
def meeting_lifecycle(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    user, data = current_user(), json_body()
    action = data.get("action")
    if action not in {
        "late", "cancel", "complete", "no_show", "dispute_no_show", "leave",
        "not_happened", "reschedule",
    }:
        return jsonify(error="Неизвестное действие"), 400
    state = state_for(meeting)
    if state.status == "completed" and not completed_by_all(meeting):
        state.status = "active"
        db.session.commit()
    if state.status != "active":
        return jsonify(error="Встреча уже завершена или отменена"), 409
    target_id = data.get("target_user_id")
    if action in {"cancel", "reschedule"} and meeting.owner_id != user.id:
        return jsonify(error="Это действие доступно создателю встречи"), 403
    if action == "not_happened" and meeting.format == "group" and meeting.owner_id != user.id:
        return jsonify(error="Групповую встречу закрывает создатель"), 403
    if action == "leave":
        if meeting.owner_id == user.id:
            return jsonify(error="Создатель может отменить встречу"), 400
        interest = Interest.query.filter_by(
            meeting_id=meeting.id, user_id=user.id, status="accepted"
        ).first()
        if not interest:
            return jsonify(error="Вы уже не участвуете в этой встрече"), 409
        interest.status = "rejected"
        clear_meeting_consents(meeting.id)
        db.session.add(MeetingEvent(meeting_id=meeting.id, user_id=user.id,
                                    kind="leave", note="Участник отказался от встречи"))
        purge_chat_if_ready(meeting)
        close_member_presences(meeting)
        purge_meeting_notifications(meeting)
        bump_sync(feed=True, user_ids={meeting.owner_id, user.id}, meeting_ids={meeting.id})
        db.session.commit()
        emit_realtime({meeting.owner_id, user.id}, "meeting_left", meeting.id)
        notify_user(
            meeting.owner_id,
            f"{user.name} отказался от встречи «{meeting.description}»",
        )
        return jsonify(ok=True, left=True)
    if action == "no_show":
        if utcnow() < normalize_dt(meeting.starts_at):
            return jsonify(error="Неявку можно отметить только после назначенного времени"), 409
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return jsonify(error="Укажите участника, который не пришёл"), 400
        if target_id == user.id or target_id not in accepted_user_ids(meeting):
            return jsonify(error="Некорректный участник"), 400
        note = str(data.get("note", "")).strip()[:180]
        if len(note) < 5:
            return jsonify(error="Коротко укажите, что произошло"), 400
        if MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=user.id,
            target_user_id=target_id, kind="no_show",
        ).first():
            return jsonify(error="Неявка уже отмечена"), 409
    if action == "dispute_no_show":
        no_show = MeetingEvent.query.filter_by(
            meeting_id=meeting.id, target_user_id=user.id, kind="no_show"
        ).order_by(MeetingEvent.id.desc()).first()
        if not no_show:
            return jsonify(error="Нет отметки о неявке для оспаривания"), 409
        if MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=user.id,
            target_user_id=no_show.user_id, kind="no_show_disputed",
        ).first():
            return jsonify(error="Отметка уже оспорена"), 409
        reason = str(data.get("note", "")).strip()[:180]
        if len(reason) < 5:
            return jsonify(error="Коротко укажите причину оспаривания"), 400
        db.session.add(MeetingEvent(
            meeting_id=meeting.id, user_id=user.id,
            target_user_id=no_show.user_id, kind="no_show_disputed", note=reason,
        ))
        dispute_report = UserReport(
            meeting_id=meeting.id,
            reporter_id=user.id,
            target_id=no_show.user_id,
            category="no_show_dispute",
            reason=reason,
            status="new",
        )
        db.session.add(dispute_report)
        db.session.flush()
        queue_admin_report(dispute_report, independent_report_count(no_show.user_id))
        bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id}, admin=True)
        db.session.commit()
        emit_meeting(meeting, "no_show_disputed")
        notify_user(
            no_show.user_id,
            f"{user.name} оспорил отметку о неявке на встречу «{meeting.description}».",
            kind="meeting_no_show_disputed",
        )
        return jsonify(ok=True, room=room_payload(meeting, user))
    if action == "cancel":
        state.status = "cancelled"
        meeting.expires_at = utcnow()
    elif action == "not_happened":
        state.status = "cancelled"
        meeting.expires_at = utcnow()
        data["note"] = "Встреча не состоялась по договорённости"
    elif action == "reschedule":
        try:
            starts_at = datetime.fromisoformat(str(data.get("starts_at", "")).replace("Z", "+00:00"))
            starts_at = normalize_dt(starts_at)
        except (TypeError, ValueError):
            return jsonify(error="Выберите новое время встречи"), 400
        if starts_at < utcnow() + timedelta(minutes=5) or starts_at > utcnow() + timedelta(days=7):
            return jsonify(error="Новое время должно быть от 5 минут до 7 дней вперёд"), 400
        MeetingConfirmation.query.filter_by(meeting_id=meeting.id).delete()
        meeting.starts_at = starts_at
        meeting.expires_at = starts_at + timedelta(minutes=60)
        data["note"] = f"Новое время: {starts_at.isoformat()}"
    elif action == "complete":
        if len(accepted_user_ids(meeting)) < 2:
            return jsonify(error="Сначала подтвердите участника встречи"), 409
        existing = MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=user.id, kind="complete"
        ).first()
        if not existing:
            db.session.add(MeetingEvent(
                meeting_id=meeting.id, user_id=user.id, kind="complete",
                note="Подтвердил, что встреча состоялась",
            ))
            db.session.flush()
        if completed_by_all(meeting):
            state.status = "completed"
            meeting.expires_at = utcnow()
            reward_completed_invites(accepted_user_ids(meeting))
        else:
            state.status = "active"
            for member_id in accepted_user_ids(meeting) - {user.id}:
                notify_user(
                    member_id,
                    f"{user.name} отметил, что встреча состоялась. Подтвердите результат в приложении.",
                    kind="meeting_confirmation",
                    dedupe_key=f"meeting-confirmation:{meeting.id}:{member_id}",
                )
    elif action == "late":
        existing_late = MeetingEvent.query.filter_by(
            meeting_id=meeting.id, user_id=user.id, kind="late"
        ).all()
        if existing_late:
            for event in existing_late:
                db.session.delete(event)
            bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
            db.session.commit()
            emit_meeting(meeting, "meeting_late")
            return jsonify(ok=True, room=room_payload(meeting, user))
    if action != "complete":
        db.session.add(MeetingEvent(meeting_id=meeting.id, user_id=user.id, target_user_id=target_id,
                                    kind=action, note=str(data.get("note", "")).strip()[:180] or None))
    if action in {"cancel", "not_happened"}:
        db.session.flush()
        purge_chat_if_ready(meeting)
        close_member_presences(meeting)
        purge_meeting_notifications(meeting)
    elif action == "complete":
        close_member_presences(meeting)
    members_for_sync = accepted_user_ids(meeting)
    bump_sync(feed=action in {"cancel", "not_happened", "complete", "reschedule"},
              user_ids=members_for_sync, meeting_ids={meeting.id},
              notification_ids=members_for_sync - {user.id})
    db.session.commit()
    emit_meeting(meeting, f"meeting_{action}")
    if action in {"cancel", "late", "no_show", "not_happened", "reschedule"} or (
        action == "complete" and completed_by_all(meeting)
    ):
        action_text = {
            "cancel": "Встреча отменена создателем",
            "complete": (
                "Встреча подтверждена обоими участниками"
                if completed_by_all(meeting)
                else f"{user.name} ждёт вашего подтверждения встречи"
            ),
            "late": f"{user.name} сообщает, что опаздывает",
            "no_show": "По встрече отмечена неявка участника",
            "not_happened": "Встреча не состоялась по договорённости",
            "reschedule": "Встреча перенесена на другое время",
        }[action]
        for member_id in accepted_user_ids(meeting) - {user.id}:
            notify_user(
                member_id,
                f"{action_text}: «{meeting.description}»",
                kind=f"meeting_{action}",
            )
    return jsonify(ok=True, room=room_payload(meeting, user))


@app.post("/api/meetings/<int:meeting_id>/feedback")
@login_required
def leave_feedback(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    completed = MeetingEvent.query.filter_by(
        meeting_id=meeting.id, user_id=current_user().id, kind="complete"
    ).first()
    if not completed:
        return jsonify(error="Оценку можно поставить после завершения встречи"), 409
    try:
        rating = int(json_body().get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if rating not in range(1, 6):
        return jsonify(error="Поставьте оценку от 1 до 5"), 400
    feedback = MeetingFeedback.query.filter_by(meeting_id=meeting.id, user_id=current_user().id).first()
    if not feedback:
        feedback = MeetingFeedback(
            meeting_id=meeting.id, user_id=current_user().id, trace=f"rating:{rating}"
        )
        db.session.add(feedback)
    else:
        feedback.trace = f"rating:{rating}"
    db.session.flush()
    purge_chat_if_ready(meeting)
    close_member_presences(meeting)
    purge_meeting_notifications(meeting, {current_user().id})
    bump_sync(feed=True, user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id})
    db.session.commit()
    emit_meeting(meeting, "feedback")
    return jsonify(ok=True, closed=True)


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
        bump_sync(user_ids=accepted_user_ids(meeting), meeting_ids={meeting.id},
                  notification_ids={receiver_id})
        db.session.commit()
        emit_meeting(meeting, "thanks")
        notify_user(receiver_id, f"{user.name} поблагодарил вас после встречи «{meeting.description}»",
                    kind="thanks",
                    dedupe_key=f"thanks:{meeting.id}:{user.id}:{receiver_id}",)
    return jsonify(ok=True, room=room_payload(meeting, user))


@app.post("/api/meetings/<int:meeting_id>/report")
@login_required
def report_user(meeting_id):
    meeting, error = meeting_room_or_error(meeting_id)
    if error:
        return error
    data, reporter = json_body(), current_user()
    operation_id = request_operation_id()
    if operation_id == "":
        return jsonify(error="Некорректный Operation ID", code="INVALID_OPERATION_ID"), 400
    if replay := replay_operation(reporter, operation_id):
        return replay
    try:
        target_id = int(data.get("target_user_id"))
    except (TypeError, ValueError):
        return jsonify(error="Укажите участника"), 400
    if target_id == reporter.id or target_id not in accepted_user_ids(meeting):
        return jsonify(error="Некорректный участник"), 400
    lock_user_actions({reporter.id, target_id})
    if replay := replay_operation(reporter, operation_id):
        return replay
    if not consume_action(reporter.id, "report", 5, 86400):
        return jsonify(error="Лимит жалоб на сегодня исчерпан"), 429
    reason = str(data.get("reason", "")).strip()[:180]
    category = str(data.get("category", "other")).strip().lower()
    if category not in REPORT_CATEGORIES - {"no_show_dispute"}:
        return jsonify(error="Некорректная категория жалобы"), 400
    if len(reason) < 3:
        return jsonify(error="Опишите причину жалобы"), 400
    if UserReport.query.filter_by(meeting_id=meeting.id, reporter_id=reporter.id, target_id=target_id).first():
        return jsonify(error="Вы уже отправили жалобу на этого участника"), 409
    report = UserReport(
        meeting_id=meeting.id,
        reporter_id=reporter.id,
        target_id=target_id,
        category=category,
        reason=reason,
        status="new",
    )
    db.session.add(report)
    if data.get("block") and not UserBlock.query.filter_by(blocker_id=reporter.id, blocked_id=target_id).first():
        db.session.add(UserBlock(blocker_id=reporter.id, blocked_id=target_id))
    db.session.flush()
    recent = utcnow() - timedelta(hours=24)
    report_count = independent_report_count(target_id, recent)
    if report_count >= 3:
        moderation = UserModeration.query.filter_by(user_id=target_id).first()
        if not moderation:
            moderation = UserModeration(user_id=target_id, hidden_until=utcnow() + timedelta(hours=24),
                                        reason="Три независимые жалобы за 24 часа")
            db.session.add(moderation)
        else:
            moderation.hidden_until = utcnow() + timedelta(hours=24)
        report.auto_hidden = 1
    queue_admin_report(report, report_count)
    payload = {"ok": True, "closed": True, "report_id": report.id, "report_count": report_count}
    bump_sync(feed=bool(data.get("block") or report.auto_hidden),
              user_ids={reporter.id, target_id}, meeting_ids={meeting.id}, admin=True)
    remember_operation(reporter, operation_id, payload, 200)
    db.session.commit()
    return jsonify(payload)


@app.get("/api/admin/reports")
@admin_required
def admin_reports():
    query = UserReport.query
    status = request.args.get("status", "").strip()
    category = request.args.get("category", "").strip()
    user_filter = request.args.get("user", "").strip()
    date_filter = request.args.get("date", "").strip()
    if status == "blocked":
        query = query.filter(UserReport.decision == "block")
    elif status == "auto_hidden":
        query = query.filter(UserReport.auto_hidden == 1)
    elif status:
        query = query.filter(UserReport.status == status)
    if category:
        query = query.filter(UserReport.category == category)
    if date_filter:
        try:
            date_start = normalize_dt(datetime.fromisoformat(date_filter))
            query = query.filter(
                UserReport.created_at >= date_start,
                UserReport.created_at < date_start + timedelta(days=1),
            )
        except ValueError:
            return jsonify(error="Некорректная дата"), 400
    reports = query.order_by(UserReport.id.desc()).limit(200).all()
    if user_filter:
        reports = [item for item in reports if user_filter in {
            anonymized_user_id(item.reporter_id), anonymized_user_id(item.target_id), str(item.id),
        }]
    return jsonify(items=[admin_report_payload(item) for item in reports])


@app.get("/api/admin/reports/<int:report_id>")
@admin_required
def admin_report(report_id):
    return jsonify(report=admin_report_payload(db.get_or_404(UserReport, report_id)))


def admin_report_payload(item):
    meeting = db.session.get(Meeting, item.meeting_id)
    actions = ModerationAction.query.filter_by(report_id=item.id).order_by(
        ModerationAction.id.desc()).limit(50).all()
    return {
        "id": item.id,
        "meeting_id": item.meeting_id,
        "meeting_status": effective_meeting_status(meeting) if meeting else "deleted",
        "category": item.category,
        "reason": item.reason,
        "status": item.status,
        "decision": item.decision,
        "decision_reason": item.decision_reason,
        "auto_hidden": bool(item.auto_hidden),
        "reporter": anonymized_user_id(item.reporter_id),
        "target": anonymized_user_id(item.target_id),
        "independent_reports": independent_report_count(item.target_id),
        "trust": trust_payload(item.target_id),
        "created_at": normalize_dt(item.created_at).isoformat(),
        "reviewed_at": normalize_dt(item.reviewed_at).isoformat() if item.reviewed_at else None,
        "history": [{
            "action": action.action,
            "reason": action.reason,
            "created_at": normalize_dt(action.created_at).isoformat(),
        } for action in actions],
    }


@app.get("/api/admin/dashboard")
@admin_required
def admin_dashboard():
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    effectively_blocked_at = utcnow() + timedelta(days=3650)
    unresolved = {"new", "in_progress"}
    return jsonify(
        csrf_token=admin_csrf_token(),
        revision=revision_value("admin"),
        counts={
            "new": UserReport.query.filter_by(status="new").count(),
            "in_progress": UserReport.query.filter_by(status="in_progress").count(),
            "auto_hidden": UserReport.query.filter_by(auto_hidden=1).count(),
            "blocked": UserModeration.query.filter(
                UserModeration.hidden_until >= effectively_blocked_at
            ).count(),
            "confirmed": UserReport.query.filter_by(status="confirmed").count(),
            "dismissed": UserReport.query.filter_by(status="dismissed").count(),
            "resolved_today": UserReport.query.filter(
                UserReport.status.in_(("confirmed", "dismissed", "resolved")),
                UserReport.reviewed_at >= today,
            ).count(),
            "unresolved": UserReport.query.filter(UserReport.status.in_(unresolved)).count(),
        },
    )


@app.post("/api/admin/reports/<int:report_id>/evidence")
@admin_required
def admin_report_evidence(report_id):
    if not verify_admin_csrf():
        return jsonify(error="CSRF-проверка не пройдена"), 403
    report = db.get_or_404(UserReport, report_id)
    admin = admin_user()
    db.session.add(ModerationAction(
        report_id=report.id,
        moderator_id=admin.id,
        action="view_evidence",
        reason="Просмотр ограниченного фрагмента чата",
    ))
    incident_at = normalize_dt(report.created_at)
    messages = ChatMessage.query.filter(
        ChatMessage.meeting_id == report.meeting_id,
        ChatMessage.created_at >= incident_at - timedelta(hours=2),
        ChatMessage.created_at <= incident_at + timedelta(hours=2),
    ).order_by(ChatMessage.id.desc()).limit(20).all()
    bump_sync(admin=True)
    db.session.commit()
    return jsonify(items=[{
        "id": message.id,
        "author": anonymized_user_id(message.user_id),
        "text": message.text,
        "created_at": normalize_dt(message.created_at).isoformat(),
    } for message in reversed(messages)])


def apply_admin_action(report, admin, action, reason):
    now = utcnow()
    if action == "take":
        report.status = "in_progress"
    elif action == "confirm":
        report.status, report.decision, report.reviewed_at = "confirmed", "confirmed", now
    elif action == "dismiss":
        report.status, report.decision, report.reviewed_at = "dismissed", "dismissed", now
    elif action == "warn":
        report.status = "in_progress"
        db.session.add(UserNotification(
            user_id=report.target_id,
            kind="moderation_warning",
            text="Администратор вынес предупреждение по правилам безопасного общения.",
            dedupe_key=f"moderation-warning:{report.id}",
        ))
    elif action in {"hide_24h", "hide_7d", "block"}:
        duration = timedelta(hours=24) if action == "hide_24h" else (
            timedelta(days=7) if action == "hide_7d" else timedelta(days=36500)
        )
        moderation = UserModeration.query.filter_by(user_id=report.target_id).first()
        if not moderation:
            moderation = UserModeration(user_id=report.target_id, hidden_until=now + duration, reason=reason)
            db.session.add(moderation)
        else:
            moderation.hidden_until = max(normalize_dt(moderation.hidden_until), now + duration)
            moderation.reason = reason
        report.status = "in_progress"
        report.decision = action
    elif action == "restore":
        UserModeration.query.filter_by(user_id=report.target_id).delete(synchronize_session=False)
        report.decision = "restored"
    elif action == "close":
        report.status, report.decision, report.reviewed_at = "resolved", "closed", now
    else:
        return False
    report.moderator_id = admin.id
    report.decision_reason = reason or report.decision_reason
    db.session.add(ModerationAction(
        report_id=report.id,
        moderator_id=admin.id,
        action=action,
        reason=reason or None,
        metadata_json=json.dumps({"target": anonymized_user_id(report.target_id)}, separators=(",", ":")),
    ))
    bump_sync(feed=action in {"hide_24h", "hide_7d", "block", "restore"},
              user_ids={report.target_id}, notification_ids={report.target_id} if action == "warn" else (),
              admin=True)
    return True


@app.post("/api/admin/reports/<int:report_id>/action")
@admin_required
def admin_report_action(report_id):
    if not verify_admin_csrf():
        return jsonify(error="CSRF-проверка не пройдена"), 403
    report = db.get_or_404(UserReport, report_id)
    action = str(json_body().get("action", "")).strip()
    reason = str(json_body().get("reason", "")).strip()[:180]
    if action != "take" and len(reason) < 3:
        return jsonify(error="Укажите причину решения"), 400
    if not apply_admin_action(report, admin_user(), action, reason):
        return jsonify(error="Неизвестное действие"), 400
    db.session.commit()
    return jsonify(ok=True, report=admin_report_payload(report))


@app.post("/api/admin/reports/<int:report_id>/decision")
@admin_required
def decide_admin_report(report_id):
    if not verify_admin_csrf():
        return jsonify(error="CSRF-проверка не пройдена"), 403
    admin = admin_user()
    report = db.get_or_404(UserReport, report_id)
    decision = str(json_body().get("decision", "")).strip()
    if decision not in {"dismissed", "confirmed"}:
        return jsonify(error="Выберите решение по жалобе"), 400
    reason = str(json_body().get("reason", f"Решение по жалобе #{report.id}"))[:180]
    apply_admin_action(report, admin, "confirm" if decision == "confirmed" else "dismiss", reason)
    db.session.commit()
    return jsonify(ok=True, decision=decision)


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
    channels = {channel: {"visits": 0, "registrations": 0} for channel in ("press", "creator", "ads", "other")}
    for visit in visits:
        channel = acquisition_channel(visit.medium)
        current = channels.setdefault(channel, {"visits": 0, "registrations": 0})
        current["visits"] += 1
    for row in Acquisition.query.filter(Acquisition.created_at >= utcnow() - timedelta(days=days)):
        current = channels.setdefault(row.channel, {"visits": 0, "registrations": 0})
        current["registrations"] += 1
    return jsonify(days=days, channels=channels, total_visits=len(visits),
                   unique_visitors=len({visit.visitor_hash for visit in visits}), items=items)


def invitation_links(token):
    telegram_url = f"https://t.me/{BOT_USERNAME}?startapp=invite_{token}"
    return {"url": telegram_url, "telegram_url": telegram_url, "pwa_url": f"{PUBLIC_URL}/?invite={token}"}


@app.post("/api/invitations/claim")
@login_required
def claim_pwa_invitation():
    return jsonify(claimed=claim_invitation(current_user(), json_body().get("token")))


def end_of_app_day(now, zone=None):
    local = now.astimezone(zone or APP_TIMEZONE)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).astimezone(timezone.utc)


@app.get("/api/invitations")
@login_required
def list_invitations():
    user = current_user()
    transaction_lock(User.query.filter_by(id=user.id), User.id)
    account = invite_account(user.id)
    db.session.commit()
    items = Invitation.query.filter_by(inviter_id=user.id).order_by(Invitation.id.desc()).all()
    rewarded = Invitation.query.filter_by(inviter_id=user.id, status="rewarded").count()
    return jsonify(available=account.available, rewarded=rewarded,
                   develops_club=rewarded > 0,
                   items=[{**invitation_links(item.token),
                           "status": item.status} for item in items])


@app.post("/api/invitations")
@login_required
def create_invitation():
    user = current_user()
    transaction_lock(User.query.filter_by(id=user.id), User.id)
    account = invite_account(user.id)
    if account.available <= 0:
        return jsonify(error="Приглашения закончились. Оно вернётся после успешной встречи приглашённого"), 409
    token = secrets.token_urlsafe(12)
    account.available -= 1
    invitation = Invitation(token=token, inviter_id=user.id, status="created")
    db.session.add(invitation)
    db.session.commit()
    return jsonify(ok=True, available=account.available, **invitation_links(token)), 201


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/about")
def about_product():
    from html import escape

    english = request.args.get("lang") == "en"
    filename = "about-en.html" if english else "about.html"
    with open(os.path.join(BASE_DIR, filename), encoding="utf-8") as source:
        return Response(source.read().replace("{{PUBLIC_URL}}", escape(PUBLIC_URL)), mimetype="text/html")


@app.get("/robots.txt")
def robots():
    return Response(f"User-agent: *\nAllow: /about\nDisallow: /api/\nDisallow: /auth/\n"
                    f"Disallow: /admin\nSitemap: {PUBLIC_URL}/sitemap.xml\n", mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    from xml.sax.saxutils import escape

    urls = "".join(f"<url><loc>{escape(PUBLIC_URL + suffix)}</loc></url>" for suffix in ("/about", "/about?lang=en"))
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    mimetype="application/xml")


@app.get("/about/qr.png")
def public_pwa_qr():
    buffer = io.BytesIO()
    qrcode.make(f"{PUBLIC_URL}/").save(buffer, format="PNG")
    return Response(buffer.getvalue(), mimetype="image/png")


@app.get("/admin")
@admin_required
def admin_page():
    admin_csrf_token()
    return send_from_directory(BASE_DIR, "admin.html")


@app.get("/sichas-apple-icon-v2.png")
def ios_app_icon():
    response = send_from_directory(BASE_DIR, "apple-touch-icon.png")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


PUBLIC_FILES = {
    "legal.html",
    "manifest.webmanifest",
    "favicon-32.png",
    "apple-touch-icon.png",
    "app-icon-192.png",
    "app-icon-512.png",
    "demo-user.svg",
    "demo-anna.svg",
    "offline.html",
    "service-worker.js",
}


@app.get("/<path:path>")
def public_files(path):
    if path not in PUBLIC_FILES:
        abort(404)
    return send_from_directory(BASE_DIR, path)


def _parse_start(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


GOOD_DEED_DIRECTIONS = {
    "community": "Сообщество",
    "environment": "Экология",
    "education": "Просвещение",
    "accessibility": "Доступная среда",
}


def _deed_payload(deed, viewer=None):
    participants = GoodDeedParticipant.query.filter_by(deed_id=deed.id).all()
    can_manage = bool(viewer and (viewer.id == deed.owner_id or viewer.telegram_id in ADMIN_TELEGRAM_IDS))
    joined = [row for row in participants if row.status in {"joined", "confirmed"}]
    confirmed = [row for row in participants if row.status == "confirmed"]
    return {
        "id": deed.id,
        "title": deed.title,
        "direction": GOOD_DEED_DIRECTIONS[deed.direction],
        "description": deed.description,
        "organizer_name": deed.organizer_name,
        "coordinator_name": deed.coordinator_name,
        "area": deed.area,
        "starts_at": normalize_dt(deed.starts_at).isoformat(),
        "duration_minutes": deed.duration_minutes,
        "capacity": deed.capacity,
        "instructions": deed.instructions,
        "status": deed.status,
        "result_summary": deed.result_summary,
        "participants": len(joined),
        "confirmed_participants": len(confirmed),
        "my_status": next((row.status for row in participants if viewer and row.user_id == viewer.id), None),
        "can_manage": can_manage,
        "mine": bool(viewer and viewer.id == deed.owner_id),
        "roster": [{"id": row.user_id, "name": db.session.get(User, row.user_id).name}
                   for row in joined] if can_manage else [],
    }


@app.get("/api/v2/good-deeds")
def list_good_deeds_v2():
    viewer = current_user()
    query = GoodDeed.query
    can_moderate = bool(viewer and viewer.telegram_id in ADMIN_TELEGRAM_IDS)
    if not can_moderate:
        visible = GoodDeed.status.in_(["active", "completed"])
        query = query.filter(visible | (GoodDeed.owner_id == viewer.id) if viewer else visible)
    deeds = query.order_by(GoodDeed.starts_at.asc()).limit(100).all()
    return jsonify(items=[_deed_payload(item, viewer) for item in deeds], can_moderate=can_moderate,
                   csrf_token=admin_csrf_token() if can_moderate else None)


@app.post("/api/v2/good-deeds")
def create_good_deed_v2():
    user = current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    if not user_profile_payload(user)["completed"]:
        return jsonify(error="Сначала заполните профиль и пройдите селфи-проверку"), 409
    data = json_body()
    direction = str(data.get("direction", "community"))
    if direction not in GOOD_DEED_DIRECTIONS:
        return jsonify(error="Выберите направление акции"), 400
    title = str(data.get("title", "")).strip()[:120]
    description = str(data.get("description", "")).strip()[:500]
    organizer = str(data.get("organizer_name", "")).strip()[:120]
    coordinator = str(data.get("coordinator_name", "")).strip()[:120]
    area = str(data.get("area", "")).strip()[:120]
    instructions = str(data.get("instructions", "")).strip()[:500]
    starts_at = _parse_start(str(data.get("starts_at", "")))
    try:
        duration = min(max(int(data.get("duration_minutes", 120)), 30), 480)
        capacity = min(max(int(data.get("capacity", 10)), 2), 100)
    except (TypeError, ValueError):
        return jsonify(error="Проверьте длительность и количество участников"), 400
    if min(map(len, [title, description, organizer, coordinator, area, instructions])) < 3 or not starts_at:
        return jsonify(error="Заполните организатора, координатора, место, время, задачу и инструкции"), 400
    if starts_at < utcnow() - timedelta(minutes=5):
        return jsonify(error="Время акции уже прошло"), 400
    if not place_is_public(area) or prohibited_activity(title, description, instructions, area) or any(re.search(
        r"квартир|подъезд|парол|банковск|паспорт|передач\w* денег|медицинск|опасн\w* работ", field.casefold()
    ) for field in (description, instructions)):
        return jsonify(error="Допустима безопасная помощь в публичном месте"), 400
    deed = GoodDeed(
        owner_id=user.id,
        title=title,
        direction=direction,
        description=description,
        organizer_name=organizer,
        coordinator_name=coordinator,
        area=area,
        starts_at=starts_at,
        duration_minutes=duration,
        capacity=capacity,
        instructions=instructions,
        status="pending",
    )
    db.session.add(deed)
    db.session.commit()
    return jsonify(ok=True, item=_deed_payload(deed, user), pending=True), 201


@app.post("/api/v2/good-deeds/<int:deed_id>/join")
def join_good_deed_v2(deed_id):
    user = current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    if not user_profile_payload(user)["completed"]:
        return jsonify(error="Сначала заполните профиль и пройдите селфи-проверку"), 409
    deed = GoodDeed.query.filter_by(id=deed_id).with_for_update().first()
    if deed is None:
        abort(404)
    if deed.status != "active" or normalize_dt(deed.starts_at) <= utcnow():
        return jsonify(error="Запись на эту акцию закрыта"), 409
    participant = GoodDeedParticipant.query.filter_by(deed_id=deed.id, user_id=user.id).first()
    desired = json_body().get("joined", not bool(participant))
    if type(desired) is not bool:
        return jsonify(error="Укажите статус участия"), 400
    if not desired:
        if participant:
            db.session.delete(participant)
    elif not participant:
        count = GoodDeedParticipant.query.filter(
            GoodDeedParticipant.deed_id == deed.id,
            GoodDeedParticipant.status.in_(["joined", "confirmed"]),
        ).count()
        if count >= deed.capacity:
            return jsonify(error="Команда уже набрана"), 409
        db.session.add(GoodDeedParticipant(deed_id=deed.id, user_id=user.id, status="joined"))
    db.session.commit()
    return jsonify(ok=True, joined=desired, item=_deed_payload(deed, user))


@app.post("/api/v2/good-deeds/<int:deed_id>/complete")
def complete_good_deed_v2(deed_id):
    user = current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    deed = GoodDeed.query.filter_by(id=deed_id).with_for_update().first()
    if deed is None:
        abort(404)
    if user.id != deed.owner_id and user.telegram_id not in ADMIN_TELEGRAM_IDS:
        return jsonify(error="Результат подтверждает организатор"), 403
    if deed.status != "active" or normalize_dt(deed.starts_at) > utcnow():
        return jsonify(error="Результат доступен после начала опубликованной акции"), 409
    data = json_body()
    result = str(data.get("result_summary", "")).strip()[:500]
    if len(result) < 5:
        return jsonify(error="Коротко опишите результат доброго дела"), 400
    try:
        hours = min(max(float(data.get("hours", deed.duration_minutes / 60)), 0.5), 12)
    except (TypeError, ValueError):
        return jsonify(error="Некорректное количество часов"), 400
    selection = data.get("participant_ids")
    if selection is not None and (
        not isinstance(selection, list) or any(type(value) is not int for value in selection)
    ):
        return jsonify(error="Выберите участников"), 400
    if not math.isfinite(hours):
        return jsonify(error="Некорректное количество часов"), 400
    participant_ids = set(selection) if selection is not None else None
    thanks = str(data.get("thanks") or "").strip()[:180]
    participants = GoodDeedParticipant.query.filter_by(deed_id=deed.id).all()
    for participant in participants:
        if participant_ids is None or participant.user_id in participant_ids:
            participant.status = "confirmed"
            participant.hours = hours
            participant.thanks = thanks or None
            participant.confirmed_at = utcnow()
    deed.status = "completed"
    deed.result_summary = result
    db.session.commit()
    return jsonify(ok=True, item=_deed_payload(deed, user))


@app.get("/api/v2/good-trace")
def good_trace_v2():
    user = current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    rows = GoodDeedParticipant.query.filter_by(user_id=user.id, status="confirmed").all()
    deed_ids = [row.deed_id for row in rows]
    deeds = GoodDeed.query.filter(GoodDeed.id.in_(deed_ids)).all() if deed_ids else []
    return jsonify(
        confirmed_deeds=len(rows),
        thanks=sum(bool(row.thanks) for row in rows),
        hours=round(sum(row.hours for row in rows), 1),
        directions=sorted({GOOD_DEED_DIRECTIONS[deed.direction] for deed in deeds}),
        appreciations=[{"deed": deed.title, "text": row.thanks}
                       for deed in deeds for row in rows if row.deed_id == deed.id and row.thanks],
    )


@app.post("/api/v2/admin/good-deeds/<int:deed_id>/decision")
@login_required
def moderate_good_deed(deed_id):
    user = current_user()
    if user.telegram_id not in ADMIN_TELEGRAM_IDS:
        return jsonify(error="Нет доступа"), 403
    if not verify_admin_csrf():
        return jsonify(error="CSRF-проверка не пройдена"), 403
    deed = transaction_lock(GoodDeed.query.filter_by(id=deed_id), GoodDeed.id)
    if not deed:
        abort(404)
    if deed.owner_id == user.id:
        return jsonify(error="Акцию проверяет независимый модератор"), 403
    decision = json_body().get("decision")
    if decision not in ("active", "rejected"):
        return jsonify(error="Выберите публикацию или отклонение"), 400
    if deed.status != "pending":
        return jsonify(error="Решение уже принято"), 409
    deed.status = decision
    db.session.add(ModerationAction(moderator_id=user.id, action="good_deed_decision",
                                    metadata_json=json.dumps({"deed_id": deed.id, "decision": decision})))
    db.session.commit()
    return jsonify(ok=True, item=_deed_payload(deed, user))


class TelegramPreference(db.Model):
    __tablename__ = "telegram_preference"
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    enabled = db.Column(db.Integer, nullable=False)
    revision = db.Column(db.Integer, nullable=False)


class CompanyWatch(db.Model):
    __tablename__ = "company_watch"
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    token = db.Column(db.String(32), nullable=False)
    category = db.Column(db.String(20), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    radius = db.Column(db.Integer, nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=False)
    matched = db.Column(db.Integer, nullable=False)


class Acquisition(db.Model):
    __tablename__ = "acquisition"
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    channel = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(60), nullable=False)
    campaign = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class PrivateTask(db.Model):
    __tablename__ = "private_task"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    category = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    budget = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, index=True)
    result = db.Column(db.String(500))
    dispute = db.Column(db.String(300))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class TaskApplication(db.Model):
    __tablename__ = "task_application"
    task_id = db.Column(db.Integer, db.ForeignKey("private_task.id"), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)


def transaction_lock(query, column):
    if engine.dialect.name == "sqlite":
        # SQLite serializes writers. Acquire the write lock before reading state.
        query.update({column: column}, synchronize_session=False)
        return query.first()
    return query.with_for_update().first()


def queue_user_telegram(user_id, kind, dedupe_key=None, watch_token=None):
    preference = db.session.get(TelegramPreference, user_id)
    if not preference or not preference.enabled:
        return False
    dedupe_key = dedupe_key or f"event:{g.request_id if has_request_context() else uuid.uuid4().hex}:{user_id}:{kind}"
    insert = postgresql_insert if engine.dialect.name == "postgresql" else sqlite_insert
    statement = insert(NotificationOutbox).values(
        kind="user_telegram", dedupe_key=f"user:{dedupe_key}",
        payload_json=json.dumps({"user_id": user_id, "revision": preference.revision,
                                 "watch": watch_token, "event": kind, "reminder": dedupe_key}),
        status="pending", attempts=0, created_at=utcnow(), next_attempt_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["dedupe_key"])
    return bool(db.session.execute(statement).rowcount)


def process_user_telegram_outbox(limit=20):
    if not BOT_TOKEN:
        return
    for _ in range(limit):
        query = NotificationOutbox.query.filter_by(kind="user_telegram", status="pending").order_by(NotificationOutbox.id)
        row = query.with_for_update(skip_locked=True).first() if engine.dialect.name == "postgresql" else query.first()
        if not row:
            break
        row_id, payload = row.id, json.loads(row.payload_json)
        reserved = NotificationOutbox.query.filter_by(id=row_id, status="pending").update(
            {"status": "sending", "attempts": 1}, synchronize_session=False
        )
        db.session.commit()
        if not reserved:
            continue
        user = transaction_lock(User.query.filter_by(id=payload["user_id"]), User.id)
        row = db.session.get(NotificationOutbox, row_id)
        if not row:
            db.session.rollback()
            continue
        preference = db.session.get(TelegramPreference, payload["user_id"])
        allowed = bool(user and user.telegram_id.isdigit() and preference and preference.enabled
                       and preference.revision == payload["revision"])
        event = payload["event"]
        if event == "presence_reminder":
            presence = Presence.query.filter_by(user_id=payload["user_id"]).first()
            allowed = allowed and bool(presence and normalize_dt(presence.active_until) > utcnow()
                                       and presence_reminder_key(presence) == payload["reminder"])
        if payload["watch"]:
            watch = db.session.get(CompanyWatch, payload["user_id"])
            allowed = allowed and bool(watch and watch.token == payload["watch"]
                                       and normalize_dt(watch.ends_at) > utcnow())
        row.status = "cancelled"
        if allowed:
            message = {
                "presence_reminder": "Вы открыты к общению уже час. Изменить статус можно в «Сейчас».",
                "company_watch": "В «Сейчас» появилась компания по вашей подписке.",
            }.get(event, "В «Сейчас» есть новое событие встречи.")
            try:
                result = telegram_api("sendMessage", {
                    "chat_id": user.telegram_id, "text": f"{message}\n{PUBLIC_URL}/",
                    "link_preview_options": {"is_disabled": True},
                })
                row.status = "sent" if result and result.get("ok") else "undelivered"
                row.delivered_at = utcnow() if row.status == "sent" else None
            except (requests.RequestException, ValueError):
                # A timeout can occur after Telegram accepts the message.
                row.status = "undelivered"
        db.session.commit()


def presence_reminder_key(presence):
    started = normalize_dt(presence.updated_at).replace(microsecond=0).isoformat()
    return f"presence-reminder:{presence.id}:{started}"


@app.route("/api/settings/telegram", methods=["GET", "POST"])
@login_required
def telegram_preferences():
    user = current_user()
    if request.method == "POST":
        enabled = json_body().get("enabled")
        if type(enabled) is not bool:
            return jsonify(error="Укажите согласие на уведомления"), 400
        if enabled and not (BOT_TOKEN and user.telegram_id.isdigit()):
            return jsonify(error="Уведомления Telegram недоступны"), 409
        transaction_lock(User.query.filter_by(id=user.id), User.id)
        preference = db.session.get(TelegramPreference, user.id)
        if not preference:
            db.session.add(TelegramPreference(user_id=user.id, enabled=int(enabled), revision=1))
        elif preference.enabled != enabled:
            preference.enabled = int(enabled)
            preference.revision += 1
        if not enabled:
            CompanyWatch.query.filter_by(user_id=user.id).delete()
        db.session.commit()
    preference = db.session.get(TelegramPreference, user.id)
    return jsonify(enabled=bool(preference and preference.enabled), available=bool(BOT_TOKEN and user.telegram_id.isdigit()),
                   bot_url=f"https://t.me/{BOT_USERNAME}")


@app.route("/api/company-watch", methods=["GET", "POST", "DELETE"])
@login_required
def company_watch():
    user = current_user()
    if request.method != "GET":
        transaction_lock(User.query.filter_by(id=user.id), User.id)
    watch = db.session.get(CompanyWatch, user.id)
    if request.method == "DELETE":
        if watch:
            db.session.delete(watch)
            db.session.commit()
        return jsonify(active=False)
    if request.method == "POST":
        if not completed_profile(user):
            return jsonify(error="Сначала заполните профиль"), 403
        preference = db.session.get(TelegramPreference, user.id)
        if not (BOT_TOKEN and preference and preference.enabled and user.telegram_id.isdigit()):
            return jsonify(error="Включите Telegram-уведомления в профиле и запустите бота"), 409
        data = json_body()
        category, radius, window = data.get("category"), data.get("radius"), data.get("window")
        zone = submitted_time_zone(data.get("time_zone"))
        if zone is None:
            return jsonify(error="Некорректный часовой пояс"), 400
        lat, lon = data.get("latitude"), data.get("longitude")
        if not isinstance(category, str) or category not in VALID_CATEGORIES or type(radius) is not int or radius not in (3, 5, 10, 12) or window not in ("hour", "today") or not valid_coordinates(lat, lon):
            return jsonify(error="Проверьте занятие, радиус, время и геолокацию"), 400
        if not consume_action(user.id, "company_watch", 5, 3600):
            return jsonify(error="Слишком много изменений подписки"), 429
        if not watch:
            watch = CompanyWatch(user_id=user.id)
            db.session.add(watch)
        now = utcnow()
        watch.token, watch.category = secrets.token_hex(16), category
        watch.latitude, watch.longitude = approximate_zone(float(lat), float(lon))
        watch.radius, watch.starts_at = radius, now
        watch.ends_at = now + timedelta(hours=1) if window == "hour" else end_of_app_day(now, zone)
        watch.matched = 0
        db.session.commit()
    active = bool(watch and not watch.matched and normalize_dt(watch.ends_at) > utcnow())
    return jsonify(active=active, matched=bool(watch and watch.matched),
                   ends_at=normalize_dt(watch.ends_at).isoformat() if watch else None,
                   category=watch.category if watch else None, radius=watch.radius if watch else None)


def process_company_watches():
    if not BOT_TOKEN:
        return
    now = utcnow()
    CompanyWatch.query.filter(CompanyWatch.ends_at <= now).delete(synchronize_session=False)
    watches = CompanyWatch.query.filter_by(matched=0).all()
    for watch in watches:
        user_id = watch.user_id
        preference = db.session.get(TelegramPreference, user_id)
        if not preference or not preference.enabled or user_hidden(user_id):
            continue
        blocks = {row.blocked_id if row.blocker_id == user_id else row.blocker_id for row in UserBlock.query.filter(
            (UserBlock.blocker_id == user_id) | (UserBlock.blocked_id == user_id)
        )}
        excluded = blocks | {user_id}
        presences = Presence.query.filter(Presence.active_until > now, Presence.category == watch.category)
        meetings = Meeting.query.filter(Meeting.category == watch.category, Meeting.expires_at > now,
                                        Meeting.starts_at < watch.ends_at)
        candidates = [(row.user_id, row.latitude, row.longitude) for row in presences]
        for meeting in meetings:
            if targeted_meeting(meeting) or effective_meeting_status(meeting) != "active" or chat_closed_for(meeting, user_id):
                continue
            if meeting.category == "help" and meeting.description not in SAFE_HELP_GOALS:
                continue
            if len(accepted_user_ids(meeting)) >= (2 if meeting.format == "one" else 6):
                continue
            if Interest.query.filter_by(meeting_id=meeting.id, user_id=user_id).first():
                continue
            candidates.append((meeting.owner_id, meeting.latitude, meeting.longitude))
        for owner_id, lat, lon in candidates:
            if owner_id in excluded or user_hidden(owner_id):
                continue
            if haversine_km(watch.latitude, watch.longitude, *approximate_zone(lat, lon)) <= watch.radius:
                updated = CompanyWatch.query.filter_by(user_id=user_id, token=watch.token, matched=0).update(
                    {"matched": 1}, synchronize_session=False
                )
                if updated:
                    queue_user_telegram(user_id, "company_watch", f"watch:{watch.token}", watch.token)
                break
    db.session.commit()


ACQUISITION_CHANNELS = {"press": "press", "creator": "creator", "ads": "ads",
                        "cpc": "ads", "paid_social": "ads"}


def acquisition_channel(medium):
    return ACQUISITION_CHANNELS.get(medium, "other")


def record_acquisition(user, source, medium, campaign):
    channel = acquisition_channel(medium)
    if not user or channel == "other" or utcnow() - normalize_dt(user.created_at) > timedelta(hours=24):
        return
    insert = postgresql_insert if engine.dialect.name == "postgresql" else sqlite_insert
    db.session.execute(insert(Acquisition).values(
        user_id=user.id, channel=channel, source=source, campaign=campaign, created_at=utcnow()
    ).on_conflict_do_nothing(index_elements=["user_id"]))


TASK_CATEGORIES = {
    "editing": "Редактура текста", "digitizing": "Оцифровка открытых материалов",
    "design": "Оформление материалов", "research": "Поиск общедоступной информации",
    "stock": "Проверка наличия и выкладки товара", "price": "Проверка цены",
    "public_info": "Проверка публичной информации на месте", "public_photo": "Разрешённая съёмка объекта",

}


def private_task_access():
    return private_task_user(current_user())


def private_task_user(user):
    allowed = {value.strip() for value in os.getenv("PRIVATE_TASK_TELEGRAM_IDS", "").split(",")}
    if not user or user_hidden(user.id):
        return None
    return user if user.telegram_id in (allowed | ADMIN_TELEGRAM_IDS | task_moderator_ids()) or corporate_name(user) else None


def task_peer_allowed(owner_id, candidate_id):
    if not private_task_user(db.session.get(User, owner_id)):
        return False
    return not UserBlock.query.filter(
        ((UserBlock.blocker_id == owner_id) & (UserBlock.blocked_id == candidate_id))
        | ((UserBlock.blocker_id == candidate_id) & (UserBlock.blocked_id == owner_id))
    ).first()


def task_item(task, user, admin):
    details = db.session.get(TaskDetails, task.id)
    manager = task.owner_id == user.id or admin
    private = manager or task.assignee_id == user.id
    applications = TaskApplication.query.filter_by(task_id=task.id).all()
    return {
        "id": task.id, "category": task.category, "title": task.title,
        "description": task.description, "budget": task.budget, "currency": details.currency if details else "BYN",
        "terms": task_terms_payload(details),
        "history": [{"action": row.action, "status": row.status, "at": normalize_dt(row.created_at).isoformat()}
                    for row in TaskActivity.query.filter_by(task_id=task.id).order_by(TaskActivity.id)] if private else [],
        "estimated_fee": f'{task.budget * (details.fee_bps if details else 0) / 10000:.2f}',
        "materials": [{"id": row.id, "url": f"/api/private/tasks/{task.id}/materials/{row.id}"}
                      for row in TaskMaterial.query.filter_by(task_id=task.id)] if private else [],
        "status": task.status, "mine": task.owner_id == user.id,
        "assigned_to_me": task.assignee_id == user.id,
        "applied": any(row.user_id == user.id for row in applications),
        "result": task.result if private else None, "dispute": task.dispute if private else None,
        "applicants": [{"id": row.user_id, "name": db.session.get(User, row.user_id).name,
                        "completed": PrivateTask.query.filter_by(assignee_id=row.user_id, status="accepted").count()}
                       for row in applications] if manager else [],
    }


@app.route("/api/private/tasks", methods=["GET", "POST"])
@login_required
def private_tasks():
    user = private_task_access()
    if not user:
        return jsonify(error="Раздел доступен участникам закрытой группы"), 403
    admin = user.telegram_id in (ADMIN_TELEGRAM_IDS | task_moderator_ids())
    if request.method == "GET":
        query = PrivateTask.query
        if not admin:
            query = query.filter((PrivateTask.owner_id == user.id) | (PrivateTask.assignee_id == user.id)
                                 | (PrivateTask.status == "approved"))
        city = request.args.get("city", "").strip()[:100]
        country = request.args.get("country", "").strip().upper()[:3]
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            return jsonify(error="Укажите двухбуквенный код страны"), 400
        if city or country:
            query = query.join(TaskDetails, TaskDetails.task_id == PrivateTask.id)
            if city:
                query = query.filter(func.lower(TaskDetails.city) == city.lower())
            if country:
                query = query.filter(TaskDetails.country == country)
        items = query.order_by(PrivateTask.id.desc()).limit(100)
        return jsonify(items=[task_item(task, user, admin) for task in items
                              if admin or user.id in (task.owner_id, task.assignee_id)
                              or task_peer_allowed(task.owner_id, user.id)],
                       categories=enabled_task_categories(), category_labels=TASK_CATEGORIES, currencies=sorted(TASK_CURRENCIES), moderator=admin, csrf_token=admin_csrf_token() if admin else None,
                       reputation={"completed": PrivateTask.query.filter_by(assignee_id=user.id, status="accepted").count()})
    if not completed_profile(user):
        return jsonify(error="Сначала заполните профиль"), 403
    if not task_policy()["enabled"]:
        return jsonify(error="Создание поручений приостановлено"), 409
    data = json_body()
    category, budget = data.get("category"), data.get("budget")
    title, description = str(data.get("title") or "").strip(), str(data.get("description") or "").strip()
    if not isinstance(category, str) or category not in enabled_task_categories() or type(budget) is not int or not 1 <= budget <= task_policy()["max_budget"]:
        return jsonify(error="Выберите разрешённую категорию и укажите сумму в BYN"), 400
    if not 3 <= len(title) <= 100 or not 10 <= len(description) <= 500:
        return jsonify(error="Укажите название и описание поручения"), 400
    terms = parse_task_terms(category, data)
    if terms is None:
        return jsonify(error="Проверьте город, страну, срок, валюту и требования к результату"), 400
    if prohibited_activity(title, description, terms["requirements"]):
        return jsonify(error="Поручение должно быть безопасным и не должно затрагивать частные данные"), 400
    if not consume_action(user.id, "private_task", 5, 3600):
        return jsonify(error="Слишком много поручений за час"), 429
    task = PrivateTask(owner_id=user.id, category=category, title=title, description=description,
                       budget=budget, status="pending")
    db.session.add(task)
    db.session.flush()
    db.session.add(TaskDetails(task_id=task.id, organization=corporate_name(user), **terms))
    db.session.add(TaskActivity(task_id=task.id, actor_id=user.id, action="create", status="pending"))
    db.session.commit()
    return jsonify(item=task_item(task, user, admin)), 201


@app.post("/api/private/tasks/<int:task_id>/action")
@login_required
def private_task_action(task_id):
    user = private_task_access()
    if not user:
        return jsonify(error="Нет доступа"), 403
    admin = user.telegram_id in (ADMIN_TELEGRAM_IDS | task_moderator_ids())
    task = transaction_lock(PrivateTask.query.filter_by(id=task_id), PrivateTask.id)
    if not task or not (admin or user.id in (task.owner_id, task.assignee_id) or (task.status == "approved" and task_peer_allowed(task.owner_id, user.id))):
        abort(404)
    data = json_body()
    action = data.get("action")
    before_status = task.status
    details = db.session.get(TaskDetails, task.id)
    if action in ("approve", "apply", "assign") and not task_policy()["enabled"]:
        return jsonify(error="Публикация и назначение поручений приостановлены"), 409
    if action in ("apply", "assign", "submit") and not completed_profile(user):
        return jsonify(error="Сначала заполните профиль"), 403
    if action in ("approve", "apply", "assign") and details and details.due_at and normalize_dt(details.due_at) <= utcnow():
        return jsonify(error="Срок поручения истёк"), 409
    owner, assignee = task.owner_id == user.id, task.assignee_id == user.id
    text = str(data.get("text") or "").strip()
    if action in ("approve", "reject", "resolve_accept", "resolve_refuse"):
        if not admin or owner or assignee or not verify_admin_csrf():
            return jsonify(error="Действие доступно независимому модератору"), 403
        transitions = {"approve": ("pending", "approved"), "reject": ("pending", "rejected"),
                       "resolve_accept": ("disputed", "accepted"), "resolve_refuse": ("disputed", "refused")}
        before, after = transitions[action]
        if task.status != before:
            return jsonify(error="Решение уже принято или действие недоступно"), 409
        task.status = after
    elif action == "apply" and not owner and task.status == "approved":
        if TaskActivity.query.filter_by(task_id=task.id, actor_id=user.id, action="approve").first():
            return jsonify(error="Модератор своего решения не может принять это поручение"), 403
        if not db.session.get(TaskApplication, (task.id, user.id)):
            db.session.add(TaskApplication(task_id=task.id, user_id=user.id))
    elif action == "assign" and owner and task.status == "approved":
        target = data.get("user_id")
        if type(target) is not int or not db.session.get(TaskApplication, (task.id, target)) or not private_task_user(db.session.get(User, target)) or not task_peer_allowed(user.id, target):
            return jsonify(error="Выберите доступного участника из откликов"), 400
        task.assignee_id, task.status = target, "assigned"
    elif action == "submit" and assignee and task.status == "assigned" and 5 <= len(text) <= 500:
        if details and details.due_at and normalize_dt(details.due_at) <= utcnow():
            return jsonify(error="Срок поручения истёк. Вы можете открыть спор"), 409
        photos = data.get("photos", [])
        if not isinstance(photos, list) or len(photos) > 3:
            return jsonify(error="Можно приложить до трёх фотографий"), 400
        try:
            images = [sanitized_task_photo(photo) for photo in photos]
        except (ValueError, OSError, Image.DecompressionBombError):
            return jsonify(error="Загрузите корректное изображение JPEG, PNG или WebP"), 400
        if task.category == "public_photo" and not images:
            return jsonify(error="Добавьте фотографию результата"), 400
        for image in images:
            db.session.add(TaskMaterial(task_id=task.id, image=encrypt_selfie(image)))
        task.result, task.status = text, "submitted"
    elif action == "accept" and owner and task.status == "submitted":
        task.status = "accepted"
    elif action == "refuse" and assignee and task.status == "assigned":
        task.status = "refused"
    elif action == "refuse" and owner and task.status == "submitted" and 5 <= len(text) <= 300:
        task.dispute, task.status = text, "refused"
    elif action == "dispute" and (
        (owner and task.status == "submitted") or
        (assignee and (task.status in ("assigned", "submitted") or (task.status == "refused" and task.result)))
    ) and 5 <= len(text) <= 300:
        task.dispute, task.status = text, "disputed"
    else:
        return jsonify(error="Действие недоступно для вашей роли или статуса поручения"), 409
    if task.status != before_status or action == "apply" and db.session.new:
        db.session.add(TaskActivity(task_id=task.id, actor_id=user.id, action=action, status=task.status))
    db.session.commit()
    return jsonify(item=task_item(task, user, admin))


def delete_private_features(user_id):
    for row in NotificationOutbox.query.filter_by(kind="user_telegram"):
        if json.loads(row.payload_json).get("user_id") == user_id:
            db.session.delete(row)
    ReleaseSetting.query.filter_by(key=f"corporate:{user_id}").delete()
    CompanyWatch.query.filter_by(user_id=user_id).delete()
    TelegramPreference.query.filter_by(user_id=user_id).delete()
    Acquisition.query.filter_by(user_id=user_id).delete()
    TaskApplication.query.filter_by(user_id=user_id).delete()
    owned_ids = db.session.query(PrivateTask.id).filter_by(owner_id=user_id)
    TaskApplication.query.filter(TaskApplication.task_id.in_(owned_ids)).delete(synchronize_session=False)
    assigned_ids = db.session.query(PrivateTask.id).filter_by(assignee_id=user_id)
    TaskMaterial.query.filter(TaskMaterial.task_id.in_(owned_ids) | TaskMaterial.task_id.in_(assigned_ids)).delete(synchronize_session=False)
    TaskActivity.query.filter(TaskActivity.task_id.in_(owned_ids) | (TaskActivity.actor_id == user_id)).delete(synchronize_session=False)
    TaskDetails.query.filter(TaskDetails.task_id.in_(owned_ids)).delete(synchronize_session=False)
    PrivateTask.query.filter_by(owner_id=user_id).delete()
    PrivateTask.query.filter_by(assignee_id=user_id).update({"assignee_id": None, "status": "refused", "result": None, "dispute": None})


class TaskDetails(db.Model):
    __tablename__ = "task_details"
    task_id = db.Column(db.Integer, db.ForeignKey("private_task.id"), primary_key=True)
    city = db.Column(db.String(100))
    organization = db.Column(db.String(120))
    country = db.Column(db.String(2))
    due_at = db.Column(db.DateTime(timezone=True))
    requirements = db.Column(db.String(500), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    fee_bps = db.Column(db.Integer, nullable=False)


class TaskMaterial(db.Model):
    __tablename__ = "task_material"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("private_task.id"), nullable=False, index=True)
    image = db.Column(db.Text, nullable=False)


class TaskActivity(db.Model):
    __tablename__ = "task_activity"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("private_task.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    action = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class ReleaseSetting(db.Model):
    __tablename__ = "release_setting"
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Text, nullable=False)


TASK_CURRENCIES = frozenset({"BYN", "EUR", "USD", "GBP", "PLN", "CAD", "JPY"})
LOCAL_TASKS = frozenset({"stock", "price", "public_info", "public_photo"})
DEFAULT_TASK_POLICY = {"categories": list(TASK_CATEGORIES), "max_budget": 10000, "fee_bps": 0, "enabled": True}


def submitted_time_zone(value):
    if value is None:
        return APP_TIMEZONE
    if not isinstance(value, str) or not 1 <= len(value) <= 70:
        return None
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def task_moderator_ids():
    return {value.strip() for value in os.getenv("TASK_MODERATOR_TELEGRAM_IDS", "").split(",") if value.strip()}


def task_policy():
    setting = db.session.get(ReleaseSetting, "task_policy")
    return json.loads(setting.value) if setting else DEFAULT_TASK_POLICY


def enabled_task_categories():
    return {key: TASK_CATEGORIES[key] for key in task_policy()["categories"]}


def prohibited_activity(*fields):
    pattern = (r"парол|банковск|паспорт|PIN[- ]?код|cvv|прослед|слежк|разыскать человека|"
               r"незаконн.{0,10}съ[её]м|персональн.{0,10}данн|перевезти неизвест|"
               r"частн.{0,10}территор|медицинск.{0,15}манипуляц|несовершеннолет|"
               r"stalk|track a person|private data|bank password|medical procedure|unknown parcel")
    return any(re.search(pattern, field, re.IGNORECASE) for field in fields)


def parse_task_terms(category, data):
    city = str(data.get("city") or "").strip()
    country = str(data.get("country") or "").strip().upper()
    requirements = str(data.get("requirements") or "").strip()
    currency = data.get("currency", "BYN")
    deadline = data.get("due_at")
    if not isinstance(currency, str) or currency not in TASK_CURRENCIES or len(city) > 100 or len(requirements) > 500:
        return None
    if bool(city) != bool(country) or country and not re.fullmatch(r"[A-Z]{2}", country):
        return None
    due_at = _parse_start(deadline) if isinstance(deadline, str) else None
    if deadline and (not due_at or not utcnow() < due_at <= utcnow() + timedelta(days=90)):
        return None
    if category in LOCAL_TASKS and not (city and due_at and len(requirements) >= 5):
        return None
    return {"city": city or None, "country": country or None, "due_at": due_at,
            "requirements": requirements, "currency": currency, "fee_bps": task_policy()["fee_bps"]}


def task_terms_payload(details):
    if not details:
        return {"city": None, "country": None, "due_at": None, "requirements": "", "fee_bps": 0, "organization": None}
    return {"city": details.city, "country": details.country, "requirements": details.requirements,
            "organization": details.organization,
            "due_at": normalize_dt(details.due_at).isoformat() if details.due_at else None,
            "fee_bps": details.fee_bps}


def sanitized_task_photo(value):
    if not valid_photo(value):
        raise ValueError("Invalid image")
    raw = base64.b64decode(value.split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as photo:
        if photo.width * photo.height > 12_000_000 or photo.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("Image size or format")
        photo.thumbnail((1440, 1440))
        # A fresh image drops EXIF, GPS coordinates and embedded profiles.
        clean = Image.new("RGB", photo.size)
        clean.paste(photo.convert("RGB"))
        buffer = io.BytesIO()
        clean.save(buffer, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def expire_task_materials():
    cutoff = utcnow() - timedelta(days=90)
    recent = db.session.query(TaskActivity.task_id).filter(TaskActivity.created_at >= cutoff)
    expired = db.session.query(PrivateTask.id).filter(
        PrivateTask.status.in_(("accepted", "refused", "rejected")),
        PrivateTask.created_at < cutoff,
        ~PrivateTask.id.in_(recent),
    )
    TaskMaterial.query.filter(TaskMaterial.task_id.in_(expired)).delete(synchronize_session=False)
    db.session.commit()


@app.get("/api/private/tasks/<int:task_id>/materials/<int:material_id>")
@login_required
def task_material_download(task_id, material_id):
    user = private_task_access()
    task = db.session.get(PrivateTask, task_id) if user else None
    if not task or (user.id not in (task.owner_id, task.assignee_id)
                    and user.telegram_id not in (ADMIN_TELEGRAM_IDS | task_moderator_ids())):
        abort(404)
    material = TaskMaterial.query.filter_by(id=material_id, task_id=task_id).first()
    value = decrypt_selfie(material.image) if material else None
    if not value:
        abort(404)
    return Response(base64.b64decode(value.split(",", 1)[1]), mimetype="image/jpeg",
                    headers={"Content-Disposition": 'attachment; filename="result.jpg"'})


@app.route("/api/admin/release-settings", methods=["GET", "POST"])
@admin_required
def release_settings():
    if request.method == "POST":
        if not verify_admin_csrf():
            return jsonify(error="CSRF-проверка не пройдена"), 403
        data = json_body()
        categories, max_budget, fee, enabled = (data.get(key) for key in DEFAULT_TASK_POLICY)
        if not isinstance(categories, list) or not categories or any(
            not isinstance(key, str) or key not in TASK_CATEGORIES for key in categories
        ) or type(max_budget) is not int or not 1 <= max_budget <= 100000 or type(fee) is not int or not 0 <= fee <= 5000 or type(enabled) is not bool:
            return jsonify(error="Проверьте параметры поручений"), 400
        policy = {"categories": sorted(set(categories)), "max_budget": max_budget, "fee_bps": fee, "enabled": enabled}
        insert = postgresql_insert if engine.dialect.name == "postgresql" else sqlite_insert
        statement = insert(ReleaseSetting).values(key="task_policy", value=json.dumps(policy))
        db.session.execute(statement.on_conflict_do_update(index_elements=["key"], set_={"value": statement.excluded.value}))
        db.session.add(ModerationAction(moderator_id=current_user().id, action="task_policy",
                                        metadata_json=json.dumps(policy)))
        db.session.commit()
    return jsonify(policy=task_policy(), categories=TASK_CATEGORIES, csrf_token=admin_csrf_token())


def corporate_name(user):
    setting = db.session.get(ReleaseSetting, f"corporate:{user.id}")
    return setting.value if setting else None


@app.route("/api/admin/corporate-customers", methods=["GET", "POST"])
@admin_required
def corporate_customers():
    if request.method == "POST":
        if not verify_admin_csrf():
            return jsonify(error="CSRF-проверка не пройдена"), 403
        data = json_body()
        account = data.get("account")
        organization = data.get("organization")
        if not isinstance(account, str) or not re.fullmatch(r"\d{1,20}", account) or not isinstance(organization, str):
            return jsonify(error="Укажите Telegram ID и организацию"), 400
        organization = organization.strip()
        if organization and not 2 <= len(organization) <= 120:
            return jsonify(error="Название организации должно содержать от 2 до 120 символов"), 400
        user = User.query.filter_by(telegram_id=account).first()
        if not user:
            return jsonify(error="Пользователь должен сначала зарегистрироваться"), 404
        transaction_lock(User.query.filter_by(id=user.id), User.id)
        key = f"corporate:{user.id}"
        existing = db.session.get(ReleaseSetting, key)
        if organization and not existing:
            db.session.add(ReleaseSetting(key=key, value=organization))
        elif organization:
            existing.value = organization
        elif existing:
            db.session.delete(existing)
        db.session.add(ModerationAction(moderator_id=current_user().id, action="corporate_access",
                                        metadata_json=json.dumps({"user_id": user.id, "enabled": bool(organization)})))
        db.session.commit()
    items = []
    for setting in ReleaseSetting.query.filter(ReleaseSetting.key.startswith("corporate:")).order_by(ReleaseSetting.key):
        user = db.session.get(User, int(setting.key.split(":")[1]))
        if user:
            items.append({"account": user.telegram_id, "organization": setting.value})
    return jsonify(items=items, csrf_token=admin_csrf_token())


@app.get("/api/admin/release-metrics")
@admin_required
def release_metrics():
    now = utcnow()
    week = now - timedelta(days=7)
    completed = MeetingState.query.filter_by(status="completed").count()
    returning = db.session.query(ActionLog.user_id).filter(ActionLog.created_at >= week).group_by(
        ActionLog.user_id
    ).having(func.count(distinct(func.date(ActionLog.created_at))) >= 2).count()
    repeat_count = Meeting.query.join(MeetingState).filter(
        MeetingState.status == "completed", Meeting.description.startswith("Повторная встреча · ")
    ).count()
    grouped = db.session.query(TaskDetails.currency, func.sum(PrivateTask.budget * TaskDetails.fee_bps)).join(
        TaskDetails, TaskDetails.task_id == PrivateTask.id
    ).filter(PrivateTask.status == "accepted").group_by(TaskDetails.currency).all()
    return jsonify(
        visits=TrafficVisit.query.count(), registrations=User.query.count(),
        active_7d=db.session.query(func.count(distinct(ActionLog.user_id))).filter(ActionLog.created_at >= week).scalar(),
        meetings=Meeting.query.count(), responses=Interest.query.count(), confirmed_responses=Interest.query.filter_by(status="accepted").count(),
        completed_meetings=completed, returning_7d=returning, completed_repeat_meetings=repeat_count, completed_good_deeds=GoodDeed.query.filter_by(status="completed").count(),
        tasks=PrivateTask.query.count(), completed_tasks=PrivateTask.query.filter_by(status="accepted").count(),
        estimated_commission=[{"currency": code, "amount": str(round(total / 10000, 2))} for code, total in grouped],
        actual_revenue=[], runtime_errors=dict(RUNTIME_ERRORS),
    )


def verify_production_schema():
    """Fail before serving traffic when Alembic migrations were not applied."""
    try:
        with engine.connect() as connection:
            versions = set(connection.execute(sql_text("SELECT version_num FROM alembic_version")).scalars())
    except Exception as error:
        raise RuntimeError(
            "Database schema revision cannot be verified; run Alembic migrations before startup"
        ) from error
    if versions != {EXPECTED_SCHEMA_REVISION}:
        raise RuntimeError(
            f"Database schema is not at required revision {EXPECTED_SCHEMA_REVISION}; "
            "run Alembic migrations before startup"
        )


if os.getenv("ALEMBIC_RUNNING") != "1":
    if IS_PRODUCTION:
        verify_production_schema()
    else:
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

if os.getenv("ALEMBIC_RUNNING") != "1" and not ALLOW_TEST_AUTH:
    threading.Thread(target=presence_reminder_loop, name="presence-reminders", daemon=True).start()
    if BOT_TOKEN:
        threading.Thread(target=notification_outbox_loop, name="notification-outbox", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
