import os
import threading
import unittest

import main


def normalized_postgres_url(value):
    value = str(value or "")
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


TEST_POSTGRES_URL = normalized_postgres_url(os.getenv("TEST_POSTGRES_URL"))
DESTRUCTIVE_TEST_DB_CONFIRMED = (
    main.engine.dialect.name == "postgresql"
    and bool(TEST_POSTGRES_URL)
    and main.database_url == TEST_POSTGRES_URL
    and os.getenv("ALLOW_DESTRUCTIVE_TEST_DB", "false").lower() == "true"
)


@unittest.skipUnless(
    DESTRUCTIVE_TEST_DB_CONFIRMED,
    "requires DATABASE_URL=TEST_POSTGRES_URL and ALLOW_DESTRUCTIVE_TEST_DB=true for an isolated PostgreSQL database",
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

    def clone_client(self, source):
        clone = main.app.test_client()
        cookie_name = main.app.config["SESSION_COOKIE_NAME"]
        cookie = source.get_cookie(cookie_name)
        self.assertIsNotNone(cookie)
        clone.set_cookie(cookie_name, cookie.value, domain="localhost")
        return clone

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
        concurrent_owners = [self.clone_client(owner) for _ in pending_ids]
        responses = self.concurrent_post([
            lambda value=value, client=client: client.post(
                f"/api/interests/{value}/decision", json={"decision": "accepted"}
            )
            for value, client in zip(pending_ids, concurrent_owners)
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

    def test_same_accept_operation_is_replayed_concurrently(self):
        owner, participant = main.app.test_client(), main.app.test_client()
        self.login(owner, 530)
        self.login(participant, 531)
        meeting_id = self.create_meeting(owner, "Одновременный повтор")
        participant.post(f"/api/meetings/{meeting_id}/interest", json={})
        interest_id = owner.get("/api/interests").get_json()["incoming"][0]["id"]
        clients = [self.clone_client(owner), self.clone_client(owner)]
        headers = {"X-Operation-ID": "concurrent-accept-operation-530"}
        responses = self.concurrent_post([
            lambda client=client: client.post(
                f"/api/interests/{interest_id}/decision",
                json={"decision": "accepted"},
                headers=headers,
            )
            for client in clients
        ])
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(sum(bool(response.get_json().get("replayed")) for response in responses), 1)
