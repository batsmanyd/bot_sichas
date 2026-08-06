import os
import threading
import unittest

import main


@unittest.skipUnless(
    main.engine.dialect.name == "postgresql" and os.getenv("TEST_POSTGRES_URL"),
    "requires an explicit isolated TEST_POSTGRES_URL",
)
class PostgresConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.app.config.update(TESTING=True)

    def setUp(self):
        main.db.session.remove()
        main.Model.metadata.drop_all(main.engine)
        main.Model.metadata.create_all(main.engine)

    def login(self, client, number):
        response = client.post("/auth/test", json={"user": str(number)})
        self.assertEqual(response.status_code, 200, response.get_json())

    def create_meeting(self, owner, description, meeting_format="one"):
        return owner.post("/api/meetings", json={
            "latitude": 53.9023, "longitude": 27.5619, "category": "cafe",
            "description": description, "format": meeting_format,
        }).get_json()["id"]

    def concurrent_post(self, calls):
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            barrier.wait()
            results[index] = call()

        threads = [threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return results

    def test_two_owners_cannot_accept_one_user(self):
        first_owner, second_owner, participant = [main.app.test_client() for _ in range(3)]
        for client, number in zip((first_owner, second_owner, participant), (501, 502, 503)):
            self.login(client, number)
        first_meeting = self.create_meeting(first_owner, "Первая встреча")
        second_meeting = self.create_meeting(second_owner, "Вторая встреча")
        participant.post(f"/api/meetings/{first_meeting}/interest", json={})
        participant.post(f"/api/meetings/{second_meeting}/interest", json={})
        first_interest = first_owner.get("/api/interests").get_json()["incoming"][0]["id"]
        second_interest = second_owner.get("/api/interests").get_json()["incoming"][0]["id"]
        responses = self.concurrent_post([
            lambda: first_owner.post(f"/api/interests/{first_interest}/decision", json={"decision": "accepted"}),
            lambda: second_owner.post(f"/api/interests/{second_interest}/decision", json={"decision": "accepted"}),
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])

    def test_group_last_place_cannot_be_overfilled(self):
        owner = main.app.test_client()
        self.login(owner, 510)
        meeting_id = self.create_meeting(owner, "Группа", "group")
        accepted_clients = []
        for number in range(511, 515):
            client = main.app.test_client()
            self.login(client, number)
            client.post(f"/api/meetings/{meeting_id}/interest", json={})
            interest_id = next(item["id"] for item in owner.get("/api/interests").get_json()["incoming"]
                               if item["status"] == "pending")
            owner.post(f"/api/interests/{interest_id}/decision", json={"decision": "accepted"})
            accepted_clients.append(client)
        pending_ids = []
        for number in (515, 516):
            client = main.app.test_client()
            self.login(client, number)
            client.post(f"/api/meetings/{meeting_id}/interest", json={})
        pending_ids = [item["id"] for item in owner.get("/api/interests").get_json()["incoming"]
                       if item["status"] == "pending"]
        responses = self.concurrent_post([
            lambda value=value: owner.post(f"/api/interests/{value}/decision", json={"decision": "accepted"})
            for value in pending_ids
        ])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(main.Interest.query.filter_by(meeting_id=meeting_id, status="accepted").count(), 5)

    def test_repeated_operation_returns_original_result(self):
        owner, participant = main.app.test_client(), main.app.test_client()
        self.login(owner, 520)
        self.login(participant, 521)
        meeting_id = self.create_meeting(owner, "Повтор операции")
        participant.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = owner.get("/api/interests").get_json()["incoming"][0]["id"]
        headers = {"X-Operation-ID": "accept-operation-520"}
        first = owner.post(f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}, headers=headers)
        replay = owner.post(f"/api/interests/{interest_id}/decision", json={"decision": "accepted"}, headers=headers)
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        self.assertTrue(replay.get_json()["replayed"])
