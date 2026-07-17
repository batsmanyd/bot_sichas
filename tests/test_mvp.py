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
        self.assertTrue(self.first.get("/api/presence").get_json()["active"])
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
        self.assertEqual(self.second.get(f"/api/meetings/{meeting_id}/room").status_code, 200)
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
        first_user = main.User.query.filter_by(telegram_id="test-1").one()
        second_user = main.User.query.filter_by(telegram_id="test-2").one()
        self.assertEqual(self.second.post(f"/api/meetings/{meeting_id}/lifecycle", json={
            "action": "late", "note": "Опоздаю на 5 минут",
        }).status_code, 200)
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

    def test_meeting_rate_limit(self):
        self.login(self.first, 10)
        point = {"latitude": 53.9023, "longitude": 27.5619}
        payload = {**point, "category": "walk", "description": "Прогуляться", "format": "one"}
        for _ in range(5):
            self.assertEqual(self.first.post("/api/meetings", json=payload).status_code, 201)
        response = self.first.post("/api/meetings", json=payload)
        self.assertEqual(response.status_code, 429, response.get_json())
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


if __name__ == "__main__":
    unittest.main()
