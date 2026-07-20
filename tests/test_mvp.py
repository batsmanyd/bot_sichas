import hashlib
import hmac
import json
import time
import unittest
from datetime import timedelta
from urllib.parse import urlencode

import main


class MvpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.app.config.update(TESTING=True)

    def setUp(self):
        main.db.session.remove()
        main.Model.metadata.drop_all(main.engine)
        main.Model.metadata.create_all(main.engine)
        self.first = main.app.test_client()
        self.second = main.app.test_client()
        self.third = main.app.test_client()

    def login(self, client, number):
        response = client.post("/auth/test", json={"user": str(number)})
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_real_two_user_flow(self):
        self.assertEqual(self.first.post("/api/presence", json={}).status_code, 401)
        self.login(self.first, 1)
        self.login(self.second, 2)
        point_one = {"latitude": 53.9023, "longitude": 27.5619}
        point_two = {"latitude": 53.9060, "longitude": 27.5680}
        self.assertEqual(self.first.post("/api/location", json=point_one).status_code, 200)
        self.assertEqual(self.second.post("/api/location", json=point_two).status_code, 200)
        response = self.first.post("/api/presence", json={**point_one, "category": "cafe"})
        self.assertEqual(response.status_code, 200, response.get_json())
        saved_presence = self.first.get("/api/presence").get_json()
        self.assertTrue(saved_presence["active"])
        self.assertEqual(saved_presence["latitude"], point_one["latitude"])
        self.assertEqual(saved_presence["longitude"], point_one["longitude"])
        response = self.second.get("/api/feed?lat=53.9060&lon=27.5680&radius=3&category=cafe")
        feed = response.get_json()["items"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["kind"], "person")
        self.assertEqual(feed[0]["name"], "Тест 1")
        self.assertNotEqual(feed[0]["latitude"], point_one["latitude"])
        response = self.first.post("/api/meetings", json={
            **point_one, "category": "walk", "description": "Прогуляться и поговорить", "format": "one",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        meeting_id = response.get_json()["id"]
        feed = self.second.get("/api/feed?lat=53.9060&lon=27.5680&radius=3&category=walk").get_json()["items"]
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["id"], meeting_id)
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/interest", json={}).status_code, 200)
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/interest", json={}).status_code, 200)
        self.assertEqual(main.Interest.query.count(), 1)
        incoming = self.first.get("/api/interests").get_json()["incoming"]
        self.assertEqual(len(incoming), 1)
        self.assertTrue(incoming[0]["can_decide"])
        interest_id = incoming[0]["id"]
        response = self.first.post(f"/api/interests/{interest_id}/decision", json={"decision": "accepted"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["interest"]["status"], "accepted")
        outgoing = self.second.get("/api/interests").get_json()["outgoing"]
        self.assertEqual(outgoing[0]["owner"]["name"], "Тест 1")
        self.assertEqual(outgoing[0]["status"], "accepted")
        self.login(self.third, 3)
        self.assertEqual(self.third.post(f"/api/meetings/{meeting_id}/interest", json={}).status_code, 409)
        first_user = main.User.query.filter_by(telegram_id="test-1").one()
        second_user = main.User.query.filter_by(telegram_id="test-2").one()
        main.db.session.add(main.ProfileSelfie(
            user_id=first_user.id, image="data:image/jpeg;base64,first", visibility="mutual", about="Люблю прогулки и общение с людьми.",
        ))
        main.db.session.add(main.ProfileSelfie(
            user_id=second_user.id, image="data:image/jpeg;base64,second", visibility="mutual", about="Люблю кофе и новые интересные места.",
        ))
        main.db.session.commit()
        room = self.second.get(f"/api/meetings/{meeting_id}/room").get_json()
        self.assertFalse(room["photos_revealed"])
        self.assertIsNone(next(person["picture"] for person in room["participants"] if not person["mine"]))
        self.assertIsNotNone(next(person["picture"] for person in room["participants"] if person["mine"]))
        room = self.second.post(f"/api/meetings/{meeting_id}/photo-consent").get_json()["room"]
        self.assertTrue(room["my_photo_consent"])
        self.assertFalse(room["photos_revealed"])
        room = self.first.post(f"/api/meetings/{meeting_id}/photo-consent").get_json()["room"]
        self.assertTrue(room["photos_revealed"])
        self.assertEqual({person["picture"] for person in room["participants"]}, {
            "data:image/jpeg;base64,first", "data:image/jpeg;base64,second",
        })
        first_selfie = main.ProfileSelfie.query.filter_by(user_id=first_user.id).one()
        first_selfie.visibility = "hidden"
        main.db.session.commit()
        room = self.second.get(f"/api/meetings/{meeting_id}/room").get_json()
        self.assertIsNone(next(person["picture"] for person in room["participants"] if not person["mine"]))
        response = self.second.post(f"/api/meetings/{meeting_id}/places", json={"title": "Кафе у парка"})
        self.assertEqual(response.status_code, 201, response.get_json())
        place_id = response.get_json()["room"]["places"][0]["id"]
        self.assertEqual(self.second.post(f"/api/places/{place_id}/vote").status_code, 200)
        self.assertEqual(self.second.post(f"/api/places/{place_id}/confirm").status_code, 403)
        self.assertEqual(self.first.post(f"/api/places/{place_id}/confirm").status_code, 200)
        response = self.second.post(f"/api/meetings/{meeting_id}/messages", json={"text": "Буду через 10 минут"})
        self.assertEqual(response.status_code, 201, response.get_json())
        room = self.first.get(f"/api/meetings/{meeting_id}/room").get_json()
        self.assertTrue(room["places"][0]["confirmed"])
        self.assertEqual(room["messages"][0]["text"], "Буду через 10 минут")
        late_response = self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "late", "note": "Опоздаю на 5 минут",
        })
        self.assertEqual(late_response.status_code, 200)
        self.assertTrue(late_response.get_json()["room"]["meeting"]["my_late"])
        late_response = self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={"action": "late"})
        self.assertEqual(late_response.status_code, 200)
        self.assertFalse(late_response.get_json()["room"]["meeting"]["my_late"])
        self.assertEqual(self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "no_show", "target_user_id": second_user.id,
        }).status_code, 200)
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "complete",
        }).status_code, 403)
        self.assertEqual(self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "complete",
        }).status_code, 200)
        response = self.second.post(f"/api/meetings/{meeting_id}/feedback", json={
            "trace": "Хорошо прогулялись",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["room"]["traces"][0]["text"], "Хорошо прогулялись")
        response = self.second.post(f"/api/meetings/{meeting_id}/report", json={
            "target_user_id": first_user.id, "reason": "Нарушил договорённость", "block": True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(main.UserReport.query.count(), 1)
        self.assertEqual(main.UserBlock.query.count(), 1)
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/report", json={
            "target_user_id": first_user.id, "reason": "Повторная жалоба",
        }).status_code, 409)
        self.assertEqual(self.second.get("/api/admin/reports").status_code, 403)
        self.assertEqual(self.second.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "rejected"}
        ).status_code, 403)
        presence = main.Presence.query.one()
        presence.active_until = main.utcnow() - timedelta(seconds=1)
        main.db.session.commit()
        feed = self.second.get("/api/feed?lat=53.9060&lon=27.5680&radius=3&category=cafe").get_json()["items"]
        self.assertEqual(feed, [])
        self.assertEqual(self.first.delete("/api/presence").status_code, 200)
        self.assertFalse(self.first.get("/api/presence").get_json()["active"])

    def test_validation(self):
        self.login(self.first, 1)
        response = self.first.post("/api/presence", json={"category": "bad", "latitude": 0, "longitude": 0})
        self.assertEqual(response.status_code, 400)

    def test_open_person_can_receive_proposal_and_participant_can_leave(self):
        self.login(self.first, 61)
        self.login(self.second, 62)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        opened = self.first.post("/api/presence", json={**point, "category": "cafe"})
        self.assertEqual(opened.status_code, 200, opened.get_json())
        presence_id = main.Presence.query.filter_by(
            user_id=main.User.query.filter_by(telegram_id="test-61").one().id
        ).one().id
        proposed = self.second.post(f"/api/presences/{presence_id}/interest", json={})
        self.assertEqual(proposed.status_code, 201, proposed.get_json())
        meeting_id = proposed.get_json()["meeting_id"]
        duplicate = self.second.post(f"/api/presences/{presence_id}/interest", json={})
        self.assertEqual(duplicate.status_code, 200, duplicate.get_json())
        self.assertTrue(duplicate.get_json()["already_sent"])
        self.assertEqual(main.Meeting.query.count(), 1)
        incoming = self.first.get("/api/interests").get_json()["incoming"]
        self.assertEqual(len(incoming), 1)
        self.assertIsNone(incoming[0]["participant"]["username"])
        accepted = self.first.post(
            f"/api/interests/{incoming[0]['id']}/decision", json={"decision": "accepted"}
        )
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        self.assertIsNone(accepted.get_json()["interest"]["participant"]["username"])
        self.assertEqual(self.second.get(f"/api/meetings/{meeting_id}/room").status_code, 200)
        left = self.second.post(
            f"/api/meetings/{meeting_id}/lifecycle", json={"action": "leave"}
        )
        self.assertEqual(left.status_code, 200, left.get_json())
        self.assertTrue(left.get_json()["left"])
        self.assertEqual(self.second.get(f"/api/meetings/{meeting_id}/room").status_code, 403)

    def test_anonymous_media_traffic_is_hashed_and_deduplicated(self):
        payload = {
            "visitor_id": "browser-visitor-123",
            "source": "onliner",
            "medium": "editorial",
            "campaign": "public_beta",
            "landing_path": "/",
        }
        first = self.first.post("/api/traffic", json=payload)
        second = self.first.post("/api/traffic", json=payload)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertTrue(first.get_json()["counted"])
        self.assertFalse(second.get_json()["counted"])
        visit = main.TrafficVisit.query.one()
        self.assertEqual(visit.source, "onliner")
        self.assertEqual(len(visit.visitor_hash), 64)
        self.assertNotIn(payload["visitor_id"], visit.visitor_hash)
        self.assertEqual(self.first.get("/api/admin/traffic").status_code, 401)
        self.login(self.first, 8)
        self.assertEqual(self.first.get("/api/admin/traffic").status_code, 403)

    def test_short_registration_profile(self):
        self.login(self.first, 31)
        self.assertFalse(self.first.get("/api/session").get_json()["profile_completed"])
        self.assertEqual(self.first.post("/api/profile", json={
            "name": "Ю", "age": 17, "gender": "male", "about": "Я люблю живое общение и прогулки.",
            "selfie": "data:image/jpeg;base64,test", "selfie_visibility": "mutual", "terms_accepted": True,
        }).status_code, 400)
        self.assertEqual(self.first.post("/api/profile", json={
            "name": "Юрий", "age": 51, "gender": "male", "about": "Я люблю живое общение и прогулки.",
            "selfie": "data:image/jpeg;base64,test", "selfie_visibility": "mutual", "terms_accepted": False,
        }).status_code, 400)
        response = self.first.post("/api/profile", json={
            "name": "Юрий", "age": 85, "gender": "male", "about": "Я люблю живое общение и прогулки.",
            "selfie": "data:image/jpeg;base64,test", "selfie_visibility": "hidden", "terms_accepted": True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["profile"]["city"], "Минск")
        stored_selfie = main.ProfileSelfie.query.one()
        self.assertTrue(stored_selfie.image.startswith("enc:"))
        self.assertNotIn("base64,test", stored_selfie.image)
        self.assertTrue(self.first.get("/api/session").get_json()["profile_completed"])
        profile = self.first.get("/api/profile").get_json()["profile"]
        self.assertEqual(profile["selfie_preview"], "data:image/jpeg;base64,test")
        self.assertEqual(profile["name"], "Юрий")
        self.assertEqual(profile["age"], 85)
        self.assertEqual(profile["gender"], "male")
        self.assertTrue(profile["selfie_present"])
        self.assertEqual(profile["selfie_visibility"], "hidden")

    def test_account_deletion_removes_profile_and_session(self):
        self.login(self.first, 41)
        response = self.first.post("/api/profile", json={
            "name": "Юрий", "age": 51, "gender": "male",
            "about": "Я люблю живое общение и прогулки.",
            "selfie": "data:image/jpeg;base64,delete-me",
            "selfie_visibility": "mutual", "terms_accepted": True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        user_id = main.User.query.filter_by(telegram_id="test-41").one().id
        self.assertEqual(self.first.delete("/api/account", json={"confirmation": "нет"}).status_code, 400)
        response = self.first.delete("/api/account", json={"confirmation": "УДАЛИТЬ"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(main.db.session.get(main.User, user_id))
        self.assertEqual(main.UserProfile.query.filter_by(user_id=user_id).count(), 0)
        self.assertEqual(main.ProfileSelfie.query.filter_by(user_id=user_id).count(), 0)
        self.assertFalse(self.first.get("/api/session").get_json()["authenticated"])

    def test_meeting_rate_limit(self):
        self.login(self.first, 10)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        payload = {**point, "category": "walk", "description": "Прогуляться", "format": "one"}
        for _ in range(5):
            self.assertEqual(self.first.post("/api/meetings", json=payload).status_code, 201)
        response = self.first.post("/api/meetings", json=payload)
        self.assertEqual(response.status_code, 429, response.get_json())

    def test_feed_shows_one_card_and_marker_per_person(self):
        self.login(self.first, 61)
        self.login(self.second, 62)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        self.assertEqual(self.first.post("/api/presence", json={**point, "category": "cafe"}).status_code, 200)
        first_meeting = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Выпить кофе или чай", "format": "one",
        })
        second_meeting = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Открыть новое место", "format": "one",
        })
        self.assertEqual(first_meeting.status_code, 201)
        self.assertEqual(second_meeting.status_code, 201)
        own_feed = self.first.get("/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe").get_json()["items"]
        self.assertEqual(own_feed, [])
        feed = self.second.get("/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe").get_json()["items"]
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["kind"], "meeting")
        self.assertEqual(feed[0]["id"], second_meeting.get_json()["id"])
        old_meeting = main.db.session.get(main.Meeting, first_meeting.get_json()["id"])
        self.assertLessEqual(main.normalize_dt(old_meeting.expires_at), main.utcnow())

    def test_group_meeting_has_six_people_total(self):
        self.login(self.first, 70)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        created = self.first.post("/api/meetings", json={
            **point, "category": "walk", "description": "Групповая прогулка", "format": "group",
        })
        meeting_id = created.get_json()["id"]
        participants = []
        for number in range(71, 77):
            client = main.app.test_client()
            self.login(client, number)
            self.assertEqual(client.post(f"/api/meetings/{meeting_id}/interest", json={}).status_code, 200)
            participants.append(client)
        incoming = self.first.get("/api/interests").get_json()["incoming"]
        for item in incoming[:5]:
            accepted = self.first.post(
                f"/api/interests/{item['id']}/decision", json={"decision": "accepted"}
            )
            self.assertEqual(accepted.status_code, 200, accepted.get_json())
        rejected = self.first.post(
            f"/api/interests/{incoming[5]['id']}/decision", json={"decision": "accepted"}
        )
        self.assertEqual(rejected.status_code, 409, rejected.get_json())
        self.assertEqual(main.Interest.query.filter_by(meeting_id=meeting_id, status="accepted").count(), 5)

    def test_now_and_within_hour_filters(self):
        self.login(self.first, 11)
        self.login(self.second, 12)
        self.login(self.third, 13)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        now_response = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Сейчас", "format": "one", "time_mode": "now",
        })
        hour_response = self.third.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Через 45 минут", "format": "one",
            "time_mode": "hour", "starts_in_minutes": 45,
        })
        self.assertEqual(now_response.status_code, 201, now_response.get_json())
        self.assertEqual(hour_response.status_code, 201, hour_response.get_json())
        now_items = self.second.get(
            "/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe&time=now"
        ).get_json()["items"]
        hour_items = self.second.get(
            "/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe&time=hour"
        ).get_json()["items"]
        self.assertEqual([item["description"] for item in now_items], ["Сейчас"])
        self.assertEqual([item["description"] for item in hour_items], ["Через 45 минут"])
        self.assertEqual(hour_items[0]["time_mode"], "hour")
        self.assertGreaterEqual(hour_items[0]["starts_in_minutes"], 44)
        invalid = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Некорректное время", "format": "one",
            "time_mode": "hour", "starts_in_minutes": 25,
        })
        self.assertEqual(invalid.status_code, 400)

    def test_invitation_returns_after_first_completed_meeting(self):
        self.login(self.first, 20)
        response = self.first.post("/api/invitations", json={})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["available"], 2)
        token = response.get_json()["url"].split("invite_", 1)[1]
        response = self.second.post("/auth/test", json={"user": "21", "invite_token": token})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.login(self.third, 22)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        response = self.second.post("/api/meetings", json={
            **point, "category": "walk", "description": "Первая встреча", "format": "one",
        })
        meeting_id = response.get_json()["id"]
        self.assertEqual(self.third.post(f"/api/meetings/{meeting_id}/interest", json={}).status_code, 200)
        interest_id = self.second.get("/api/interests").get_json()["incoming"][0]["id"]
        self.assertEqual(self.second.post(f"/api/interests/{interest_id}/decision", json={
            "decision": "accepted",
        }).status_code, 200)
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "complete",
        }).status_code, 200)
        invitations = self.first.get("/api/invitations").get_json()
        self.assertEqual(invitations["available"], 3)
        self.assertEqual(invitations["items"][0]["status"], "rewarded")
        response = self.first.post("/api/meetings", json={"category": "walk", "description": ""})
        self.assertEqual(response.status_code, 400)

    def test_telegram_mini_app_signature(self):
        original_token = main.BOT_TOKEN
        main.BOT_TOKEN = "123456:test-token"
        try:
            values = {
                "auth_date": str(int(time.time())),
                "query_id": "AAE-test",
                "user": json.dumps({"id": 777, "first_name": "Юрий", "username": "yuri"}, separators=(",", ":")),
            }
            check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
            secret = hmac.new(b"WebAppData", main.BOT_TOKEN.encode(), hashlib.sha256).digest()
            values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
            response = self.first.post("/auth/telegram-mini-app", json={"init_data": urlencode(values)})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["user"]["name"], "Юрий")
            values["hash"] = "0" * 64
            response = self.second.post("/auth/telegram-mini-app", json={"init_data": urlencode(values)})
            self.assertEqual(response.status_code, 401)
        finally:
            main.BOT_TOKEN = original_token

    def test_device_token_restores_session_without_telegram_confirmation(self):
        self.login(self.first, 91)
        token_response = self.first.get("/auth/device-token")
        self.assertEqual(token_response.status_code, 200, token_response.get_json())
        token = token_response.get_json()["device_token"]
        self.assertEqual(self.first.post("/auth/logout").status_code, 200)
        self.assertFalse(self.first.get("/api/session").get_json()["authenticated"])

        restore = self.first.post("/auth/device", json={"device_token": token})
        self.assertEqual(restore.status_code, 200, restore.get_json())
        session_data = self.first.get("/api/session").get_json()
        self.assertTrue(session_data["authenticated"])
        self.assertEqual(session_data["user"]["name"], "Тест 91")

        self.assertEqual(
            self.second.post("/auth/device", json={"device_token": token + "broken"}).status_code,
            401,
        )

    def test_standalone_app_receives_one_time_telegram_handoff(self):
        original_token = main.BOT_TOKEN
        main.BOT_TOKEN = "123456:test-token"
        try:
            created = self.first.post("/auth/handoff", json={})
            self.assertEqual(created.status_code, 201, created.get_json())
            handoff_token = created.get_json()["handoff_token"]
            self.assertRegex(handoff_token, r"^\d{8}$")
            self.assertEqual(created.get_json()["login_code"], handoff_token)
            self.assertEqual(created.get_json()["telegram_url"], "https://t.me/vmeste_rjadom_bot")

            pending = self.first.post(f"/auth/handoff/{handoff_token}", json={})
            self.assertEqual(pending.status_code, 202, pending.get_json())

            values = {
                "auth_date": str(int(time.time())),
                "query_id": "AAE-handoff",
                "start_param": f"login_{handoff_token}",
                "user": json.dumps({"id": 991, "first_name": "Юрий"}, separators=(",", ":")),
            }
            check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
            secret = hmac.new(b"WebAppData", main.BOT_TOKEN.encode(), hashlib.sha256).digest()
            values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
            telegram = self.second.post("/auth/telegram-mini-app", json={"init_data": urlencode(values)})
            self.assertEqual(telegram.status_code, 200, telegram.get_json())
            self.assertTrue(telegram.get_json()["handoff_claimed"])

            completed = self.first.post(f"/auth/handoff/{handoff_token}", json={})
            self.assertEqual(completed.status_code, 200, completed.get_json())
            self.assertTrue(completed.get_json()["authenticated"])
            self.assertTrue(self.first.get("/api/session").get_json()["authenticated"])
        finally:
            main.BOT_TOKEN = original_token

    def test_standalone_handoff_can_arrive_through_bot_web_app_button(self):
        original_token = main.BOT_TOKEN
        main.BOT_TOKEN = "123456:test-token"
        try:
            created = self.first.post("/auth/handoff", json={}).get_json()
            handoff_token = created["handoff_token"]
            values = {
                "auth_date": str(int(time.time())),
                "query_id": "AAE-button-handoff",
                "user": json.dumps({"id": 992, "first_name": "Анна"}, separators=(",", ":")),
            }
            check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
            secret = hmac.new(b"WebAppData", main.BOT_TOKEN.encode(), hashlib.sha256).digest()
            values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
            telegram = self.second.post("/auth/telegram-mini-app", json={
                "init_data": urlencode(values), "handoff": f"login_{handoff_token}",
            })
            self.assertEqual(telegram.status_code, 200, telegram.get_json())
            self.assertTrue(telegram.get_json()["handoff_claimed"])
            self.assertEqual(self.first.post(
                f"/auth/handoff/{handoff_token}", json={}
            ).status_code, 200)
        finally:
            main.BOT_TOKEN = original_token

    def test_standalone_code_can_be_confirmed_by_bot_message(self):
        original_token, original_api = main.BOT_TOKEN, main.telegram_api
        main.BOT_TOKEN = "123456:test-token"
        sent = []
        main.telegram_api = lambda method, payload: sent.append((method, payload))
        try:
            created = self.first.post("/auth/handoff", json={}).get_json()
            code = created["login_code"]
            response = self.second.post("/telegram/webhook", json={"message": {
                "chat": {"id": 992}, "from": {"id": 992, "first_name": "Анна"}, "text": code,
            }}, headers={"X-Telegram-Bot-Api-Secret-Token": main.TELEGRAM_WEBHOOK_SECRET})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertIn("Вход подтверждён", sent[-1][1]["text"])
            completed = self.first.post(f"/auth/handoff/{code}", json={})
            self.assertEqual(completed.status_code, 200, completed.get_json())
            self.assertTrue(completed.get_json()["authenticated"])
        finally:
            main.BOT_TOKEN, main.telegram_api = original_token, original_api

    def test_frontend_processes_handoff_even_when_telegram_session_already_exists(self):
        with open("index.html", encoding="utf-8") as source:
            frontend = source.read()
        self.assertIn("const mustPassHandoff=startParam.startsWith('login_')", frontend)
        self.assertIn("if(tg?.initData&&(!data.authenticated||mustPassHandoff))", frontend)
        self.assertIn("data-presence", frontend)
        self.assertIn("reportParticipant", frontend)
        self.assertIn("setInterval(refreshLiveData,15000)", frontend)
        self.assertIn("document.addEventListener('visibilitychange',resumeStandaloneLogin)", frontend)
        self.assertIn("tg.close()", frontend)
        self.assertIn("startBotCodeLogin", frontend)
        self.assertIn('id="closeMeeting"', frontend)
        self.assertIn("meetingTouchStart", frontend)
        self.assertIn(".meeting-sheet{", frontend)
        self.assertIn("openPlacePicker", frontend)
        self.assertIn("Выбрать на карте", frontend)
        self.assertGreaterEqual(frontend.count("attributionControl.setPrefix(false)"), 2)
        self.assertIn("maximum-scale=1,user-scalable=no", frontend)
        self.assertIn("syncVisualViewport", frontend)
        self.assertIn("meetings.filter(item=>!item.mine)", frontend)
        self.assertIn("fallback=currentLocation||{latitude:53.9023,longitude:27.5619}", frontend)
        self.assertIn("placePickerMap=L.map('placePickerMap');placePickerMap.attributionControl.setPrefix(false)", frontend)
        with open("main.py", encoding="utf-8") as source:
            self.assertNotIn('"scope": "openid profile phone"', source.read())


if __name__ == "__main__":
    unittest.main()
