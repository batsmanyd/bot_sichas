"""Safety and good-deeds v2 overlay for the existing «Сейчас» application.

The legacy application remains intact. This module imports it, adds new tables and
replaces only the endpoints whose behaviour changed in the approved 21.07.2026
product logic. Railway can run this branch with ``gunicorn app_v2:app``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from flask import Response, jsonify

import main as legacy

app = legacy.app
db = legacy.db


class ProfileMedia(db.Model):
    __tablename__ = "profile_media_v2"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    public_kind = db.Column(db.String(20), nullable=False, default="neutral")
    public_image = db.Column(db.Text)
    real_photo = db.Column(db.Text)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=legacy.utcnow, onupdate=legacy.utcnow)


class MeetingConfirmation(db.Model):
    __tablename__ = "meeting_confirmation_v2"
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=legacy.utcnow)
    __table_args__ = (legacy.UniqueConstraint("meeting_id", "user_id", name="uq_meeting_confirmation_v2"),)


class GoodDeed(db.Model):
    __tablename__ = "good_deed_v2"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
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
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=legacy.utcnow)


class GoodDeedParticipant(db.Model):
    __tablename__ = "good_deed_participant_v2"
    id = db.Column(db.Integer, primary_key=True)
    deed_id = db.Column(db.Integer, db.ForeignKey("good_deed_v2.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="joined", index=True)
    hours = db.Column(db.Float, nullable=False, default=0)
    joined_at = db.Column(db.DateTime(timezone=True), nullable=False, default=legacy.utcnow)
    confirmed_at = db.Column(db.DateTime(timezone=True))
    __table_args__ = (legacy.UniqueConstraint("deed_id", "user_id", name="uq_good_deed_participant_v2"),)


with app.app_context():
    db.create_all()
    legacy.ProfileSelfie.query.filter(legacy.ProfileSelfie.visibility != "hidden").update(
        {"visibility": "hidden"}, synchronize_session=False
    )
    db.session.commit()


_legacy_api_session = app.view_functions["api_session"]
_legacy_feed = app.view_functions["feed"]
_legacy_delete_account = app.view_functions["delete_account"]
_legacy_propose_place = app.view_functions["propose_place"]
_legacy_health = app.view_functions["health"]


def _profile_ready(user):
    return bool(
        user
        and legacy.UserProfile.query.filter_by(user_id=user.id).first()
        and legacy.ProfileSelfie.query.filter_by(user_id=user.id).first()
    )


def _media(user_id, create=False):
    item = ProfileMedia.query.filter_by(user_id=user_id).first()
    if not item and create:
        item = ProfileMedia(user_id=user_id, public_kind="neutral")
        db.session.add(item)
    return item


def _validate_image(value, label):
    if not value:
        return None
    if not re.match(r"^data:image/(?:jpeg|png|webp);base64,", value) or len(value) > 700_000:
        raise ValueError(f"Не удалось обработать {label}. Выберите другое изображение")
    return value


def _encode_image(value):
    return legacy.encrypt_selfie(value) if value else None


def _decode_image(value):
    return legacy.decrypt_selfie(value) if value else None


def _zone(lat, lon):
    lat_step, lon_step = 0.007, 0.011
    return round(round(lat / lat_step) * lat_step, 4), round(round(lon / lon_step) * lon_step, 4), 500


def _distance_band(distance):
    limits = [0.3, 0.7, 1.5, 3.0, 5.0, 10.0, 12.0]
    labels = ["до 300 м", "300–700 м", "0,7–1,5 км", "1,5–3 км", "3–5 км", "5–10 км", "до 12 км"]
    for limit, label in zip(limits, labels):
        if distance <= limit:
            return label, limit
    return "дальше 12 км", 12.0


def _owner_for_feed_item(item):
    if item.get("kind") == "person":
        presence = db.session.get(legacy.Presence, item.get("id"))
        return presence.user_id if presence else None, presence.latitude if presence else None, presence.longitude if presence else None
    meeting = db.session.get(legacy.Meeting, item.get("id"))
    return meeting.owner_id if meeting else None, meeting.latitude if meeting else None, meeting.longitude if meeting else None


def user_profile_payload_v2(user):
    profile = legacy.UserProfile.query.filter_by(user_id=user.id).first()
    selfie = legacy.ProfileSelfie.query.filter_by(user_id=user.id).first()
    media = _media(user.id)
    return {
        "completed": bool(profile and selfie),
        "name": user.name,
        "age": profile.age if profile else None,
        "gender": profile.gender if profile else None,
        "city": profile.city if profile else "Минск",
        "about": selfie.about if selfie else "",
        "selfie_present": bool(selfie),
        "selfie_preview": _decode_image(selfie.image) if selfie else None,
        "selfie_visibility": "hidden",
        "verification_label": "Селфи-проверка пройдена" if selfie else "Селфи-проверка не пройдена",
        "public_image_kind": media.public_kind if media else "neutral",
        "public_image_preview": _decode_image(media.public_image) if media and media.public_image else None,
        "real_photo_present": bool(media and media.real_photo),
        "real_photo_preview": _decode_image(media.real_photo) if media and media.real_photo else None,
    }


def api_session_v2():
    response = _legacy_api_session()
    data = response.get_json()
    user = legacy.current_user()
    if user:
        selfie = legacy.ProfileSelfie.query.filter_by(user_id=user.id).first()
        media = _media(user.id)
        data["profile_completed"] = _profile_ready(user)
        data["selfie_verified"] = bool(selfie)
        data["verification_label"] = "Селфи-проверка пройдена" if selfie else None
        data["public_image"] = _decode_image(media.public_image) if media and media.public_image else None
        data["public_image_kind"] = media.public_kind if media else "neutral"
    data["logic_version"] = "safety-good-deeds-v2"
    return jsonify(data)


def get_profile_v2():
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    return jsonify(profile=user_profile_payload_v2(user))


def save_profile_v2():
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    data = legacy.json_body()
    name = str(data.get("name", "")).strip()[:40]
    try:
        age = int(data.get("age"))
    except (TypeError, ValueError):
        return jsonify(error="Укажите возраст"), 400
    gender = str(data.get("gender", ""))
    about = str(data.get("about", "")).strip()[:160]
    selfie_image = str(data.get("selfie", ""))
    public_kind = str(data.get("public_image_kind", "neutral"))
    public_image = str(data.get("public_image", ""))
    real_photo = str(data.get("real_photo", ""))

    if len(name) < 2:
        return jsonify(error="Укажите имя — не короче двух букв"), 400
    if not 18 <= age <= 100:
        return jsonify(error="Приложение доступно пользователям от 18 до 100 лет"), 400
    if gender not in {"male", "female"}:
        return jsonify(error="Выберите вариант в поле «Пол»"), 400
    if len(about) < 20:
        return jsonify(error="Напишите одну короткую фразу о себе — минимум 20 символов"), 400
    if public_kind not in {"real", "avatar", "neutral"}:
        return jsonify(error="Выберите тип публичного изображения"), 400
    if data.get("terms_accepted") is not True:
        return jsonify(error="Подтвердите правила безопасных встреч"), 400

    profile = legacy.UserProfile.query.filter_by(user_id=user.id).first()
    selfie = legacy.ProfileSelfie.query.filter_by(user_id=user.id).first()
    media = _media(user.id, create=True)
    try:
        checked_selfie = _validate_image(selfie_image, "селфи")
        checked_public = _validate_image(public_image, "публичное изображение")
        checked_real = _validate_image(real_photo, "настоящее фото")
    except ValueError as error:
        return jsonify(error=str(error)), 400

    if not checked_selfie and not selfie:
        return jsonify(error="Добавьте свежее проверочное селфи"), 400
    if public_kind != "neutral" and not checked_public and not media.public_image:
        return jsonify(error="Добавьте публичное изображение или выберите нейтральный вариант"), 400

    if not profile:
        profile = legacy.UserProfile(user_id=user.id, age=age, gender=gender, city="Минск")
        db.session.add(profile)
    else:
        profile.age = age
        profile.gender = gender
        profile.terms_accepted_at = legacy.utcnow()

    if not selfie:
        selfie = legacy.ProfileSelfie(
            user_id=user.id,
            image=_encode_image(checked_selfie),
            visibility="hidden",
            about=about,
        )
        db.session.add(selfie)
    else:
        if checked_selfie:
            selfie.image = _encode_image(checked_selfie)
        selfie.visibility = "hidden"
        selfie.about = about

    media.public_kind = public_kind
    if public_kind == "neutral":
        media.public_image = None
    elif checked_public:
        media.public_image = _encode_image(checked_public)
    if checked_real:
        media.real_photo = _encode_image(checked_real)

    user.name = name
    db.session.commit()
    return jsonify(ok=True, profile=user_profile_payload_v2(user))


def feed_v2():
    response = _legacy_feed()
    data = response.get_json()
    items = data.get("items", [])
    user = legacy.current_user()
    if not user:
        summary = {}
        for item in items:
            key = item.get("category", "other")
            summary[key] = summary.get(key, 0) + 1
        return jsonify(items=[], guest=True, activity_summary=summary, total_activity=sum(summary.values()))

    for item in items:
        owner_id, exact_lat, exact_lon = _owner_for_feed_item(item)
        if owner_id:
            media = _media(owner_id)
            item["public_image"] = _decode_image(media.public_image) if media and media.public_image else None
            item["public_image_kind"] = media.public_kind if media else "neutral"
            item["verification_label"] = "Селфи-проверка пройдена" if item.get("profile_verified") else None
        if exact_lat is not None and exact_lon is not None:
            zone_lat, zone_lon, zone_radius = _zone(exact_lat, exact_lon)
            item["latitude"] = zone_lat
            item["longitude"] = zone_lon
            item["zone_radius_m"] = zone_radius
            item["approximate_zone"] = True
        band, upper = _distance_band(float(item.get("distance_km") or 0))
        item["distance_band"] = band
        item["distance_km"] = upper
    return jsonify(items=items, guest=False)


def room_payload_v2(meeting, user):
    room = legacy.room_payload(meeting, user)
    member_ids = legacy.accepted_user_ids(meeting)
    consented_ids = {row.user_id for row in legacy.PhotoConsent.query.filter_by(meeting_id=meeting.id).all()}
    all_have_photo = all(bool(_media(uid) and _media(uid).real_photo) for uid in member_ids)
    photos_revealed = bool(member_ids) and member_ids.issubset(consented_ids) and all_have_photo
    participants = []
    for uid in member_ids:
        participant = db.session.get(legacy.User, uid)
        media = _media(uid)
        selfie = legacy.ProfileSelfie.query.filter_by(user_id=uid).first()
        public_picture = _decode_image(media.public_image) if media and media.public_image else None
        real_picture = _decode_image(media.real_photo) if media and media.real_photo else None
        participants.append({
            "id": uid,
            "name": participant.name,
            "mine": uid == user.id,
            "public_picture": public_picture,
            "public_image_kind": media.public_kind if media else "neutral",
            "picture": real_picture if (uid == user.id or photos_revealed) else None,
            "real_photo_present": bool(real_picture),
            "photo_visible": bool(real_picture and (uid == user.id or photos_revealed)),
            "photo_consented": uid in consented_ids,
            "photo_visibility": "mutual",
            "selfie_verified": bool(selfie),
            "verification_label": "Селфи-проверка пройдена" if selfie else None,
        })

    confirmed_ids = {row.user_id for row in MeetingConfirmation.query.filter_by(meeting_id=meeting.id).all()}
    all_confirmed = bool(member_ids) and member_ids.issubset(confirmed_ids)
    confirmed_place = legacy.MeetingPlace.query.filter_by(meeting_id=meeting.id, confirmed=1).first()
    room["participants"] = participants
    room["photos_revealed"] = photos_revealed
    room["my_photo_consent"] = user.id in consented_ids
    room["mutual_photo_used"] = True
    room["meeting"]["latitude"] = None
    room["meeting"]["longitude"] = None
    room["meeting"]["my_confirmed"] = user.id in confirmed_ids
    room["meeting"]["all_confirmed"] = all_confirmed
    room["meeting"]["confirmed_place"] = confirmed_place.title if (all_confirmed and confirmed_place) else None
    room["meeting"]["confirmation_count"] = len(confirmed_ids)
    room["meeting"]["member_count"] = len(member_ids)
    return room


def get_meeting_room_v2(meeting_id):
    if not legacy.current_user():
        return jsonify(error="Нужна регистрация через Telegram"), 401
    meeting, error = legacy.meeting_room_or_error(meeting_id)
    return error or jsonify(room_payload_v2(meeting, legacy.current_user()))


def consent_to_photo_v2(meeting_id):
    if not legacy.current_user():
        return jsonify(error="Нужна регистрация через Telegram"), 401
    meeting, error = legacy.meeting_room_or_error(meeting_id)
    if error:
        return error
    user = legacy.current_user()
    media = _media(user.id)
    if not media or not media.real_photo:
        return jsonify(error="Сначала добавьте отдельное настоящее фото в профиле"), 409
    consent = legacy.PhotoConsent.query.filter_by(meeting_id=meeting.id, user_id=user.id).first()
    if not consent:
        db.session.add(legacy.PhotoConsent(meeting_id=meeting.id, user_id=user.id))
        db.session.commit()
    return jsonify(ok=True, room=room_payload_v2(meeting, user))


@app.post("/api/v2/meetings/<int:meeting_id>/confirm")
def confirm_meeting_v2(meeting_id):
    if not legacy.current_user():
        return jsonify(error="Нужна регистрация через Telegram"), 401
    meeting, error = legacy.meeting_room_or_error(meeting_id)
    if error:
        return error
    user = legacy.current_user()
    row = MeetingConfirmation.query.filter_by(meeting_id=meeting.id, user_id=user.id).first()
    if row:
        db.session.delete(row)
    else:
        db.session.add(MeetingConfirmation(meeting_id=meeting.id, user_id=user.id))
    db.session.commit()
    return jsonify(ok=True, room=room_payload_v2(meeting, user))


def propose_place_v2(meeting_id):
    title = str(legacy.json_body().get("title", "")).strip().lower()
    private_markers = ("квартира", "подъезд", "у меня дома", "у тебя дома", "мой дом", "частный дом")
    if any(marker in title for marker in private_markers):
        return jsonify(error="Для первой встречи выберите публичное место"), 400
    return _legacy_propose_place(meeting_id)


def _parse_start(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _deed_payload(deed, viewer=None):
    participants = GoodDeedParticipant.query.filter_by(deed_id=deed.id).all()
    joined = [row for row in participants if row.status in {"joined", "confirmed"}]
    confirmed = [row for row in participants if row.status == "confirmed"]
    return {
        "id": deed.id,
        "title": deed.title,
        "description": deed.description,
        "organizer_name": deed.organizer_name,
        "coordinator_name": deed.coordinator_name,
        "area": deed.area,
        "starts_at": legacy.normalize_dt(deed.starts_at).isoformat(),
        "duration_minutes": deed.duration_minutes,
        "capacity": deed.capacity,
        "instructions": deed.instructions,
        "status": deed.status,
        "result_summary": deed.result_summary,
        "participants": len(joined),
        "confirmed_participants": len(confirmed),
        "my_status": next((row.status for row in participants if viewer and row.user_id == viewer.id), None),
        "can_manage": bool(viewer and (viewer.id == deed.owner_id or viewer.telegram_id in legacy.ADMIN_TELEGRAM_IDS)),
        "organizer_verified": deed.status in {"active", "completed"},
    }


@app.get("/api/v2/good-deeds")
def list_good_deeds_v2():
    viewer = legacy.current_user()
    query = GoodDeed.query
    if not viewer or viewer.telegram_id not in legacy.ADMIN_TELEGRAM_IDS:
        query = query.filter(GoodDeed.status.in_(["active", "completed"]))
    deeds = query.order_by(GoodDeed.starts_at.asc()).limit(100).all()
    return jsonify(items=[_deed_payload(item, viewer) for item in deeds])


@app.post("/api/v2/good-deeds")
def create_good_deed_v2():
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    if not _profile_ready(user):
        return jsonify(error="Сначала заполните профиль и пройдите селфи-проверку"), 409
    data = legacy.json_body()
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
    if starts_at < legacy.utcnow() - timedelta(minutes=5):
        return jsonify(error="Время акции уже прошло"), 400
    is_admin = user.telegram_id in legacy.ADMIN_TELEGRAM_IDS
    deed = GoodDeed(
        owner_id=user.id,
        title=title,
        description=description,
        organizer_name=organizer,
        coordinator_name=coordinator,
        area=area,
        starts_at=starts_at,
        duration_minutes=duration,
        capacity=capacity,
        instructions=instructions,
        status="active" if is_admin else "pending",
    )
    db.session.add(deed)
    db.session.commit()
    return jsonify(ok=True, item=_deed_payload(deed, user), pending=not is_admin), 201


@app.post("/api/v2/good-deeds/<int:deed_id>/join")
def join_good_deed_v2(deed_id):
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    if not _profile_ready(user):
        return jsonify(error="Сначала заполните профиль и пройдите селфи-проверку"), 409
    deed = db.get_or_404(GoodDeed, deed_id)
    if deed.status != "active":
        return jsonify(error="Запись на эту акцию закрыта"), 409
    participant = GoodDeedParticipant.query.filter_by(deed_id=deed.id, user_id=user.id).first()
    if participant:
        db.session.delete(participant)
        joined = False
    else:
        count = GoodDeedParticipant.query.filter(
            GoodDeedParticipant.deed_id == deed.id,
            GoodDeedParticipant.status.in_(["joined", "confirmed"]),
        ).count()
        if count >= deed.capacity:
            return jsonify(error="Команда уже набрана"), 409
        db.session.add(GoodDeedParticipant(deed_id=deed.id, user_id=user.id, status="joined"))
        joined = True
    db.session.commit()
    return jsonify(ok=True, joined=joined, item=_deed_payload(deed, user))


@app.post("/api/v2/good-deeds/<int:deed_id>/complete")
def complete_good_deed_v2(deed_id):
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    deed = db.get_or_404(GoodDeed, deed_id)
    if user.id != deed.owner_id and user.telegram_id not in legacy.ADMIN_TELEGRAM_IDS:
        return jsonify(error="Результат подтверждает организатор"), 403
    data = legacy.json_body()
    result = str(data.get("result_summary", "")).strip()[:500]
    if len(result) < 5:
        return jsonify(error="Коротко опишите результат доброго дела"), 400
    try:
        hours = min(max(float(data.get("hours", deed.duration_minutes / 60)), 0.5), 12)
    except (TypeError, ValueError):
        return jsonify(error="Некорректное количество часов"), 400
    participant_ids = {int(value) for value in data.get("participant_ids", []) if str(value).isdigit()}
    participants = GoodDeedParticipant.query.filter_by(deed_id=deed.id).all()
    for participant in participants:
        if not participant_ids or participant.user_id in participant_ids:
            participant.status = "confirmed"
            participant.hours = hours
            participant.confirmed_at = legacy.utcnow()
    deed.status = "completed"
    deed.result_summary = result
    db.session.commit()
    return jsonify(ok=True, item=_deed_payload(deed, user))


@app.get("/api/v2/good-trace")
def good_trace_v2():
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    rows = GoodDeedParticipant.query.filter_by(user_id=user.id, status="confirmed").all()
    deed_ids = [row.deed_id for row in rows]
    deeds = GoodDeed.query.filter(GoodDeed.id.in_(deed_ids)).all() if deed_ids else []
    return jsonify(
        confirmed_deeds=len(rows),
        hours=round(sum(row.hours for row in rows), 1),
        directions=sorted({deed.title for deed in deeds})[:6],
    )


def delete_account_v2():
    user = legacy.current_user()
    if user:
        MeetingConfirmation.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        GoodDeedParticipant.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        owned = GoodDeed.query.filter_by(owner_id=user.id).all()
        for deed in owned:
            GoodDeedParticipant.query.filter_by(deed_id=deed.id).delete(synchronize_session=False)
            db.session.delete(deed)
        ProfileMedia.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.commit()
    return _legacy_delete_account()


def health_v2():
    response = _legacy_health()
    data = response.get_json()
    data["logic_version"] = "safety-good-deeds-v2"
    return jsonify(data)


def index_v2():
    with open(os.path.join(legacy.BASE_DIR, "index.html"), "r", encoding="utf-8") as source:
        html = source.read()
    replacements = {
        "private-repo-check-20260718": "safety-good-deeds-v2-20260721",
        "Можно всё посмотреть без регистрации": "Без регистрации видны интерфейс и общая активность — без персональных карточек",
        "Люди и встречи рядом": "Цели и встречи рядом",
        "Разделы, встречи и настоящая карта Минска доступны без регистрации.": "Без регистрации доступны разделы и общая активность по зонам. Персональные карточки открываются после входа.",
        "Другие люди видят только примерное расстояние и намеренно смещённую точку на карте.": "Другие люди видят только приблизительную зону и диапазон расстояния, а не точную или смещённую точку.",
        "Фото — по выбранному правилу": "Аватар публично, настоящее фото — взаимно",
        "Владелец анкеты решает: взаимно, после принятия встречи или никому.": "Проверочное селфи закрыто. Отдельное настоящее фото открывается только после согласия обеих сторон.",
        "0.14.0 · полный безопасный сценарий встречи": "0.15.0 · безопасность и добрые дела v2",
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    html = html.replace("</head>", '<link rel="stylesheet" href="/v2.css?v=20260721"></head>')
    html = html.replace("</body>", '<script src="/v2.js?v=20260721"></script></body>')
    return Response(html, mimetype="text/html")


app.view_functions["api_session"] = api_session_v2
app.view_functions["get_profile"] = get_profile_v2
app.view_functions["save_profile"] = save_profile_v2
app.view_functions["feed"] = feed_v2
app.view_functions["get_meeting_room"] = get_meeting_room_v2
app.view_functions["consent_to_photo"] = consent_to_photo_v2
app.view_functions["propose_place"] = propose_place_v2
app.view_functions["delete_account"] = delete_account_v2
app.view_functions["health"] = health_v2
app.view_functions["index"] = index_v2


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
