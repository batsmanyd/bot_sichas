import os
from datetime import timedelta

if os.getenv("ALLOW_TEST_AUTH") != "true" or not os.getenv("DATABASE_URL", "").startswith("sqlite:///"):
    raise SystemExit("Browser fixtures require a disposable SQLite database and test authentication.")

import main

main.app.testing = True
main.ADMIN_TELEGRAM_IDS = {"test-993"}
with main.app.app_context():
    if main.User.query.count():
        raise SystemExit("The browser fixture database must be empty.")
    for suffix in ("991", "992", "993"):
        user = main.User(name=f"Фикстура {suffix}",telegram_id=f"test-{suffix}")
        main.db.session.add(user)
        main.db.session.flush()
        main.db.session.add(main.UserProfile(user_id=user.id,age=30,gender="female"))
        main.db.session.add(main.ProfileSelfie(user_id=user.id,image=main.encrypt_selfie("browser fixture"),about="Синтетическая анкета для проверки"))
    main.db.session.commit()

@main.app.post("/__clock/good-deed")
def advance_good_deed():
    if not main.admin_user():
        return {"ok":False},403
    main.GoodDeed.query.update({"starts_at":main.utcnow()-timedelta(hours=3)})
    main.db.session.commit()
    return {"ok":True}

main.app.run(host="127.0.0.1", port=int(os.environ.get("ACCEPTANCE_PORT", "8095")))
