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
            "note": "Не пришёл и не предупредил",
        }).status_code, 200)
        pending_completion = self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "complete",
        })
        self.assertEqual(pending_completion.status_code, 200)
        self.assertEqual(pending_completion.get_json()["room"]["meeting"]["status"], "active")
        self.assertTrue(pending_completion.get_json()["room"]["meeting"]["my_completion_confirmed"])
        pending_list = self.second.get("/api/interests").get_json()["outgoing"][0]
        self.assertEqual(pending_list["meeting_status"], "active")
        self.assertTrue(pending_list["my_completion_confirmed"])
        response = self.second.post(f"/api/meetings/{meeting_id}/feedback", json={
            "rating": 5,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["closed"])
        self.assertEqual(self.second.get(f"/api/meetings/{meeting_id}/room").status_code, 410)
        self.assertEqual(self.second.get("/api/interests").get_json()["outgoing"], [])
        self.assertEqual(main.Presence.query.filter_by(user_id=second_user.id).count(), 0)
        self.assertEqual(self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "complete",
        }).status_code, 200)
        response = self.first.post(f"/api/meetings/{meeting_id}/report", json={
            "target_user_id": second_user.id, "reason": "Нарушил договорённость", "block": True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["closed"])
        self.assertEqual(self.first.get(f"/api/meetings/{meeting_id}/room").status_code, 410)
        self.assertEqual(main.ChatMessage.query.count(), 1)
        response = self.first.post(f"/api/meetings/{meeting_id}/report", json={
            "target_user_id": first_user.id, "reason": "Нарушил договорённость", "block": True,
        })
        self.assertEqual(response.status_code, 410, response.get_json())
        self.assertEqual(main.UserReport.query.count(), 1)
        self.assertEqual(main.UserBlock.query.count(), 1)
        self.assertEqual(self.first.post(f"/api/meetings/{meeting_id}/report", json={
            "target_user_id": second_user.id, "reason": "Повторная жалоба",
        }).status_code, 410)
        self.assertEqual(self.second.get("/api/admin/reports").status_code, 403)
        self.assertEqual(self.second.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "rejected"}
        ).status_code, 403)
        self.assertEqual(self.first.post(
            "/api/presence", json={**point_one, "category": "cafe"}
        ).status_code, 200)
        presence = main.Presence.query.one()
        presence.active_until = main.utcnow() - timedelta(seconds=1)
        main.db.session.commit()
        feed = self.second.get("/api/feed?lat=53.9060&lon=27.5680&radius=3&category=cafe").get_json()["items"]
        self.assertEqual(feed, [])
        self.assertEqual(self.first.delete("/api/presence").status_code, 200)
        self.assertFalse(self.first.get("/api/presence").get_json()["active"])

    def test_chat_is_deleted_after_ratings_and_after_24_hours(self):
        self.login(self.first, 120)
        self.login(self.second, 121)
        point = {"latitude": 53.9023, "longitude": 27.5619}

        def accepted_meeting(description):
            meeting_id = self.first.post("/api/meetings", json={
                **point, "category": "walk", "description": description, "format": "one",
            }).get_json()["id"]
            self.second.post(f"/api/meetings/{meeting_id}/interest", json={})
            interest_id = self.first.get("/api/interests").get_json()["incoming"][0]["id"]
            self.first.post(
                f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
            )
            return meeting_id

        meeting_id = accepted_meeting("Проверка удаления после оценок")
        self.second.post(
            f"/api/meetings/{meeting_id}/messages", json={"text": "Удалить после оценок"}
        )
        self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={"action": "complete"})
        self.first.post(f"/api/meetings/{meeting_id}/feedback", json={"rating": 5})
        self.assertEqual(main.ChatMessage.query.filter_by(meeting_id=meeting_id).count(), 1)
        self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={"action": "complete"})
        self.second.post(f"/api/meetings/{meeting_id}/feedback", json={"rating": 4})
        self.assertEqual(main.ChatMessage.query.filter_by(meeting_id=meeting_id).count(), 0)
        self.assertEqual(self.first.get("/api/interests").get_json()["owned"], [])
        self.assertEqual(self.second.get("/api/interests").get_json()["outgoing"], [])

        expired_id = accepted_meeting("Проверка удаления через сутки")
        self.second.post(
            f"/api/meetings/{expired_id}/messages", json={"text": "Удалить через сутки"}
        )
        self.first.post(f"/api/meetings/{expired_id}/lifecycle", json={"action": "complete"})
        completion = main.MeetingEvent.query.filter_by(
            meeting_id=expired_id, kind="complete"
        ).one()
        completion.created_at = main.utcnow() - timedelta(hours=25)
        main.db.session.commit()
        self.second.get("/api/interests")
        self.assertEqual(main.ChatMessage.query.filter_by(meeting_id=expired_id).count(), 0)
        self.assertEqual(self.second.get(f"/api/meetings/{expired_id}/room").status_code, 410)

    def test_closed_meeting_removes_stale_presence_and_related_notifications(self):
        self.login(self.first, 130)
        self.login(self.second, 131)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        self.first.post("/api/presence", json={**point, "category": "cafe"})
        self.second.post("/api/presence", json={**point, "category": "cafe"})
        meeting_id = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Выпить кофе или чай", "format": "one",
        }).get_json()["id"]
        self.second.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = self.first.get("/api/interests").get_json()["incoming"][0]["id"]
        self.first.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
        )
        first_user = main.User.query.filter_by(telegram_id="test-130").one()
        second_user = main.User.query.filter_by(telegram_id="test-131").one()
        # Reproduce a stale public status and old chat notifications from production.
        for user in (first_user, second_user):
            presence = main.Presence.query.filter_by(user_id=user.id).one()
            presence.active_until = main.utcnow() + timedelta(days=1)
        main.db.session.add(main.UserNotification(
            user_id=first_user.id, kind="info", text=f"{second_user.name}: Ты где?"
        ))
        main.db.session.add(main.UserNotification(
            user_id=first_user.id, kind="presence_reminder", text="Оставить статус включённым?"
        ))
        main.db.session.commit()
        self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={"action": "complete"})
        response = self.first.post(f"/api/meetings/{meeting_id}/feedback", json={"rating": 5})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(main.Presence.query.filter(
            main.Presence.user_id.in_({first_user.id, second_user.id})
        ).count(), 0)
        notices = self.first.get("/api/notifications").get_json()["items"]
        self.assertEqual(notices, [])

    def test_existing_closed_meeting_is_cleaned_on_next_open(self):
        self.login(self.first, 132)
        self.login(self.second, 133)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        self.second.post("/api/presence", json={**point, "category": "cafe"})
        meeting_id = self.second.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Старая встреча", "format": "one",
        }).get_json()["id"]
        self.first.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = self.second.get("/api/interests").get_json()["incoming"][0]["id"]
        self.second.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
        )
        first_user = main.User.query.filter_by(telegram_id="test-132").one()
        second_user = main.User.query.filter_by(telegram_id="test-133").one()
        main.db.session.add(main.MeetingFeedback(
            meeting_id=meeting_id, user_id=first_user.id, trace="rating:5",
            created_at=main.utcnow(),
        ))
        presence = main.Presence.query.filter_by(user_id=second_user.id).one()
        presence.active_until = main.utcnow() + timedelta(days=1)
        presence.updated_at = main.utcnow() - timedelta(minutes=10)
        main.db.session.add(main.UserNotification(
            user_id=first_user.id, kind="info", text=f"{second_user.name}: Ты где?"
        ))
        main.db.session.commit()
        feed = self.first.get(
            "/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe"
        ).get_json()["items"]
        self.assertEqual(feed, [])
        self.assertEqual(main.Presence.query.filter_by(user_id=second_user.id).count(), 0)
        self.assertEqual(self.first.get("/api/notifications").get_json()["items"], [])

    def test_stale_accepted_meeting_is_closed_after_24_hours(self):
        self.login(self.first, 137)
        self.login(self.second, 138)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        meeting_id = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Старая тестовая встреча",
            "format": "one",
        }).get_json()["id"]
        self.second.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = self.first.get("/api/interests").get_json()["incoming"][0]["id"]
        self.first.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
        )
        meeting = main.db.session.get(main.Meeting, meeting_id)
        meeting.starts_at = main.utcnow() - timedelta(hours=25)
        main.db.session.add(main.ChatMessage(
            meeting_id=meeting_id,
            user_id=main.User.query.filter_by(telegram_id="test-137").one().id,
            text="Старое тестовое сообщение",
        ))
        main.db.session.commit()

        self.assertEqual(self.first.get("/api/interests").get_json()["owned"], [])
        self.assertEqual(self.second.get("/api/interests").get_json()["outgoing"], [])
        self.assertEqual(self.first.get(f"/api/meetings/{meeting_id}/room").status_code, 410)
        self.assertEqual(main.ChatMessage.query.filter_by(meeting_id=meeting_id).count(), 0)

    def test_version_endpoint_matches_visible_build(self):
        response = self.first.get("/api/version")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["version"], main.APP_VERSION)
        page = self.first.get("/").get_data(as_text=True)
        self.assertIn(f"Версия <span id=\"appVersion\">{main.APP_VERSION}</span>", page)

    def test_confirmed_one_to_one_meeting_disappears_from_public_feed(self):
        self.login(self.first, 134)
        self.login(self.second, 135)
        self.login(self.third, 136)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        meeting_id = self.second.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Выпить кофе или чай", "format": "one",
        }).get_json()["id"]
        self.first.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = self.second.get("/api/interests").get_json()["incoming"][0]["id"]
        self.second.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
        )
        feed_url = "/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe"
        self.assertEqual(self.first.get(feed_url).get_json()["items"], [])
        self.assertEqual(self.third.get(feed_url).get_json()["items"], [])

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
        self.assertFalse(self.first.get("/api/presence").get_json()["active"])
        reopened = self.first.post("/api/presence", json={**point, "category": "cafe"})
        self.assertEqual(reopened.status_code, 200, reopened.get_json())
        self.assertTrue(self.first.get("/api/presence").get_json()["active"])
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

    def test_owner_can_manage_active_meeting_before_first_response(self):
        self.login(self.first, 63)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        created = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Выпить кофе или чай", "format": "one",
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        meeting_id = created.get_json()["id"]
        interests = self.first.get("/api/interests").get_json()
        self.assertEqual(len(interests["owned"]), 1)
        self.assertEqual(interests["owned"][0]["meeting_id"], meeting_id)
        self.assertEqual(interests["owned"][0]["accepted_count"], 0)
        room = self.first.get(f"/api/meetings/{meeting_id}/room")
        self.assertEqual(room.status_code, 200, room.get_json())
        cancelled = self.first.post(
            f"/api/meetings/{meeting_id}/lifecycle", json={"action": "cancel"}
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.get_json())
        self.assertEqual(self.first.get("/api/interests").get_json()["owned"], [])

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
        self.assertEqual(self.third.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "complete",
        }).status_code, 200)
        invitations = self.first.get("/api/invitations").get_json()
        self.assertEqual(invitations["available"], 3)
        self.assertEqual(invitations["items"][0]["status"], "rewarded")
        self.assertTrue(invitations["develops_club"])
        self.assertEqual(invitations["rewarded"], 1)
        response = self.first.post("/api/meetings", json={"category": "walk", "description": ""})
        self.assertEqual(response.status_code, 400)

    def test_presence_stays_open_and_creates_one_hour_reminder(self):
        self.login(self.first, 101)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        opened = self.first.post("/api/presence", json={**point, "category": "cafe"})
        self.assertEqual(opened.status_code, 200, opened.get_json())
        self.assertIsNone(opened.get_json()["active_until"])
        presence = main.Presence.query.one()
        self.assertGreater(
            main.normalize_dt(presence.active_until),
            main.utcnow() + timedelta(days=3000),
        )
        presence.updated_at = main.utcnow() - timedelta(minutes=61)
        main.db.session.commit()
        main.process_presence_reminders()
        main.process_presence_reminders()
        self.assertEqual(main.UserNotification.query.filter_by(kind="presence_reminder").count(), 1)
        state = self.first.get("/api/presence").get_json()
        self.assertTrue(state["active"])
        self.assertIsNone(state["active_until"])

    def test_place_is_saved_as_map_object(self):
        self.login(self.first, 102)
        self.login(self.second, 103)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        created = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Выпить кофе или чай", "format": "one",
        })
        meeting_id = created.get_json()["id"]
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/interest", json={}).status_code, 200)
        interest_id = self.first.get("/api/interests").get_json()["incoming"][0]["id"]
        self.assertEqual(self.first.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
        ).status_code, 200)
        response = self.second.post(f"/api/meetings/{meeting_id}/places", json={
            "title": "Кафе у парка", "latitude": 53.9031, "longitude": 27.5627,
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        place = response.get_json()["room"]["places"][0]
        self.assertEqual(place["latitude"], 53.9031)
        self.assertEqual(place["longitude"], 27.5627)
        self.assertIn("openstreetmap.org", place["map_url"])
        self.assertEqual(main.MeetingPlaceLocation.query.count(), 1)

    def test_place_deduplicates_auto_confirms_and_chat_stays_in_app(self):
        self.login(self.first, 108)
        self.login(self.second, 109)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        meeting_id = self.first.post("/api/meetings", json={
            **point, "category": "cafe", "description": "Выпить кофе", "format": "one",
        }).get_json()["id"]
        self.second.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = self.first.get("/api/interests").get_json()["incoming"][0]["id"]
        self.first.post(
            f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}
        )

        proposed = self.second.post(f"/api/meetings/{meeting_id}/places", json={
            "title": "Кафе у парка", "latitude": 53.9031, "longitude": 27.5627,
        })
        self.assertEqual(proposed.status_code, 201, proposed.get_json())
        place_id = proposed.get_json()["room"]["places"][0]["id"]
        self.assertEqual(proposed.get_json()["room"]["places"][0]["votes"], 1)

        agreed = self.first.post(f"/api/meetings/{meeting_id}/places", json={
            "title": "Та же точка", "latitude": 53.90311, "longitude": 27.56271,
        })
        self.assertEqual(agreed.status_code, 200, agreed.get_json())
        self.assertTrue(agreed.get_json()["already_exists"])
        self.assertEqual(main.MeetingPlace.query.count(), 1)
        self.assertEqual(main.PlaceVote.query.filter_by(place_id=place_id).count(), 2)
        self.assertTrue(agreed.get_json()["room"]["places"][0]["confirmed"])

        owner_feed = self.first.get(
            "/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe&time=now"
        ).get_json()
        guest_feed = self.second.get(
            "/api/feed?lat=53.9023&lon=27.5619&radius=3&category=cafe&time=now"
        ).get_json()
        self.assertEqual(owner_feed["agreed_places"][0]["meeting_id"], meeting_id)
        self.assertEqual(guest_feed["agreed_places"][0]["meeting_id"], meeting_id)

        notifications_before_chat = main.UserNotification.query.count()
        sent = self.second.post(
            f"/api/meetings/{meeting_id}/messages", json={"text": "Я уже иду"}
        )
        self.assertEqual(sent.status_code, 201, sent.get_json())
        self.assertEqual(main.UserNotification.query.count(), notifications_before_chat)
        self.assertEqual(
            self.first.get(f"/api/meetings/{meeting_id}/room").get_json()["messages"][0]["text"],
            "Я уже иду",
        )
        owner_interests = self.first.get("/api/interests").get_json()
        guest_interests = self.second.get("/api/interests").get_json()
        self.assertEqual(owner_interests["owned"][0]["latest_message"]["text"], "Я уже иду")
        self.assertFalse(owner_interests["owned"][0]["latest_message"]["mine"])
        self.assertEqual(guest_interests["outgoing"][0]["latest_message"]["text"], "Я уже иду")
        self.assertTrue(guest_interests["outgoing"][0]["latest_message"]["mine"])
        self.assertEqual(owner_interests["owned"][0]["people"], ["Тест 109"])
        self.assertEqual(guest_interests["outgoing"][0]["people"], ["Тест 108"])

    def test_no_show_dispute_and_thanks_trust(self):
        self.login(self.first, 104)
        self.login(self.second, 105)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        meeting_id = self.first.post("/api/meetings", json={
            **point, "category": "walk", "description": "Прогуляться вместе", "format": "one",
        }).get_json()["id"]
        self.second.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = self.first.get("/api/interests").get_json()["incoming"][0]["id"]
        self.first.post(f"/api/interests/{interest_id}/decision", json={"decision": "accepted"})
        second_user = main.User.query.filter_by(telegram_id="test-105").one()
        missing_reason = self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "no_show", "target_user_id": second_user.id,
        })
        self.assertEqual(missing_reason.status_code, 400)
        marked = self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "no_show", "target_user_id": second_user.id,
            "note": "Не пришёл и не предупредил",
        })
        self.assertEqual(marked.status_code, 200, marked.get_json())
        pending_trust = main.trust_payload(second_user.id)
        self.assertEqual(pending_trust["completed_meetings"], 0)
        self.assertEqual(pending_trust["level"], "Новый участник")
        self.assertEqual(pending_trust["no_shows"], 0)
        no_show = main.MeetingEvent.query.filter_by(kind="no_show").one()
        no_show.created_at = main.utcnow() - timedelta(hours=25)
        main.db.session.commit()
        self.assertEqual(main.trust_payload(second_user.id)["no_shows"], 1)
        room = self.second.get(f"/api/meetings/{meeting_id}/room").get_json()
        self.assertTrue(room["meeting"]["can_dispute_no_show"])
        disputed = self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "dispute_no_show", "note": "Встречу заранее перенесли в чате",
        })
        self.assertEqual(disputed.status_code, 200, disputed.get_json())
        self.assertFalse(disputed.get_json()["room"]["meeting"]["can_dispute_no_show"])
        self.assertEqual(main.trust_payload(second_user.id)["no_shows"], 0)
        self.first.post(f"/api/meetings/{meeting_id}/lifecycle", json={"action": "complete"})
        self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={"action": "complete"})
        thanked = self.first.post(f"/api/meetings/{meeting_id}/thanks", json={
            "target_user_id": second_user.id,
        })
        self.assertEqual(thanked.status_code, 200, thanked.get_json())
        trust = main.trust_payload(second_user.id)
        self.assertEqual(trust["completed_meetings"], 1)
        self.assertEqual(trust["thanks"], 1)
        self.assertEqual(trust["no_shows"], 0)
        notifications = self.second.get("/api/notifications").get_json()
        self.assertEqual(notifications["unread"], 0)
        self.assertEqual(notifications["items"], [])

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
        self.assertIn("activeRoom&&$('roomDialog').open?2500", frontend)
        self.assertIn("scheduleLiveRefresh()", frontend)
        self.assertIn("groupedMeetings(items)", frontend)
        self.assertIn("cache:'no-store'", frontend)
        self.assertIn("await loadInterests(true)", frontend)
        self.assertNotIn("Версия приложения", frontend)
        self.assertNotIn("Имитировать реальную встречу", frontend)
        self.assertIn('id="cityLabel" class="city">Минск</div>', frontend)
        self.assertIn("Участник не пришёл", frontend)
        self.assertIn("Оспорить неявку", frontend)
        self.assertIn("Сказать спасибо", frontend)
        self.assertIn('id="roomPersonCard"', frontend)
        self.assertIn("Открыть фото друг другу", frontend)
        self.assertIn('id="notificationPanel"', frontend)
        self.assertNotIn("Отметить прочитанными", frontend)
        self.assertNotIn("Участники и управление встречей", frontend)
        self.assertIn("function emptyMeetingsHtml()", frontend)
        self.assertIn("Активных встреч нет", frontend)
        self.assertIn("group.items.some(x=>x.my_completion_confirmed)", frontend)
        self.assertIn("$('messageComposer').style.display=archived?'none':'flex'", frontend)
        self.assertIn("function rateMeeting(rating)", frontend)
        self.assertIn("demoRoom.meeting.needs_feedback=true", frontend)
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
        self.assertIn("mapMeetings.filter(item=>!item.mine)", frontend)
        self.assertIn("agreedPlaces.map", frontend)
        self.assertIn("Место согласовано ✓", frontend)
        self.assertIn("&time=now`);mapMeetings=data.items", frontend)
        self.assertIn("fallback=currentLocation||{latitude:53.9023,longitude:27.5619}", frontend)
        self.assertIn("placePickerMap=L.map('placePickerMap',{zoomControl:true});placePickerMap.attributionControl.setPrefix(false)", frontend)
        self.assertIn("Ваша активная встреча", frontend)
        self.assertIn("item.age?item.age+' лет'", frontend)
        self.assertEqual(frontend.count("$('locateMe').onclick=locateMe"), 1)
        with open("main.py", encoding="utf-8") as source:
            self.assertNotIn('"scope": "openid profile phone"', source.read())


if __name__ == "__main__":
    unittest.main()
