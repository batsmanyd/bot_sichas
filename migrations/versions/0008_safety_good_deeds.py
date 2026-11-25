"""Separate private meeting photos and add confirmations and group actions."""

import json

from alembic import context, op
import sqlalchemy as sa

revision = "0008_safety_good_deeds"
down_revision = "0007_runtime_guards"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_media_v2",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False),
        sa.Column("public_kind", sa.String(20), nullable=False),
        sa.Column("public_image", sa.Text),
        sa.Column("real_photo", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_profile_media_v2_user_id", "profile_media_v2", ["user_id"], unique=True)
    op.create_table(
        "meeting_confirmation_v2",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("meeting_id", sa.Integer, sa.ForeignKey("meeting.id"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("meeting_id", "user_id", name="uq_meeting_confirmation_v2"),
    )
    op.create_table(
        "good_deed_v2",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("direction", sa.String(40), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("organizer_name", sa.String(120), nullable=False),
        sa.Column("coordinator_name", sa.String(120), nullable=False),
        sa.Column("area", sa.String(120), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer, nullable=False),
        sa.Column("capacity", sa.Integer, nullable=False),
        sa.Column("instructions", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result_summary", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "good_deed_participant_v2",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("deed_id", sa.Integer, sa.ForeignKey("good_deed_v2.id"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("hours", sa.Float, nullable=False),
        sa.Column("thanks", sa.String(180)),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("deed_id", "user_id", name="uq_good_deed_participant_v2"),
    )
    for table, columns in (
        ("meeting_confirmation_v2", ("meeting_id", "user_id")),
        ("good_deed_v2", ("owner_id", "starts_at", "status")),
        ("good_deed_participant_v2", ("deed_id", "user_id", "status")),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.execute("UPDATE profile_selfie SET visibility = 'hidden'")
    op.execute("DELETE FROM photo_consent")
    if context.is_offline_mode():
        # The offline dialect cannot iterate old payloads. Keep online upgrade
        # mandatory when upgrading a database with operation responses.
        return
    from main import decrypt_selfie, encrypt_selfie

    connection = op.get_bind()
    selfies = sa.table("profile_selfie", sa.column("id"), sa.column("image"))
    for row in connection.execute(sa.select(selfies)):
        versioned = row.image.startswith("enc:") and ":" in row.image[4:]
        if not versioned:
            image = decrypt_selfie(row.image)
            if not image:
                raise RuntimeError("Legacy selfie decryption failed; verify the original session secret")
            connection.execute(selfies.update().where(selfies.c.id == row.id).values(
                image=encrypt_selfie(image)
            ))
    records = sa.table("operation_record", sa.column("id"), sa.column("response_json"))
    for row in connection.execute(sa.select(records).where(records.c.response_json.contains('"room":'))):
        payload = json.loads(row.response_json)
        room = payload.pop("room", None)
        if room:
            payload["_room_meeting_id"] = room["meeting"]["id"]
            connection.execute(records.update().where(records.c.id == row.id).values(
                response_json=json.dumps(payload, ensure_ascii=False)
            ))


def downgrade():
    for table in ("good_deed_participant_v2", "good_deed_v2", "meeting_confirmation_v2", "profile_media_v2"):
        op.drop_table(table)
