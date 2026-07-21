"""Safe production entry point for the v2 branch.

It prevents an early v2 prototype migration from rewriting legacy selfie visibility,
then imports the v2 overlay and adds the final validation/moderation guards.
"""

from __future__ import annotations

from flask import jsonify
from sqlalchemy.orm import Query

import main as legacy


_original_query_update = Query.update


def _guarded_query_update(query, values, synchronize_session="auto", update_args=None):
    entities = {description.get("entity") for description in query.column_descriptions}
    if legacy.ProfileSelfie in entities and values == {"visibility": "hidden"}:
        return 0
    return _original_query_update(
        query,
        values,
        synchronize_session=synchronize_session,
        update_args=update_args,
    )


try:
    Query.update = _guarded_query_update
    import app_v2 as v2
finally:
    Query.update = _original_query_update


app = v2.app


_original_delete_account_v2 = v2.delete_account_v2


def delete_account_guarded():
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    if legacy.json_body().get("confirmation") != "УДАЛИТЬ":
        return jsonify(error="Подтвердите удаление аккаунта"), 400
    return _original_delete_account_v2()


@app.post("/api/v2/admin/good-deeds/<int:deed_id>/decision")
def moderate_good_deed_v2(deed_id):
    user = legacy.current_user()
    if not user:
        return jsonify(error="Нужна регистрация через Telegram"), 401
    if user.telegram_id not in legacy.ADMIN_TELEGRAM_IDS:
        return jsonify(error="Нет доступа"), 403
    deed = legacy.db.get_or_404(v2.GoodDeed, deed_id)
    decision = str(legacy.json_body().get("decision", ""))
    if decision not in {"active", "rejected"}:
        return jsonify(error="Выберите публикацию или отклонение"), 400
    deed.status = decision
    legacy.db.session.commit()
    return jsonify(ok=True, item=v2._deed_payload(deed, user))


app.view_functions["delete_account"] = delete_account_guarded
