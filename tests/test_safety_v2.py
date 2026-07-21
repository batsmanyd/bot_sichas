import unittest
from datetime import timedelta

import safe_app as app_v2


class SafetyGoodDeedsV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_v2.app.config.update(TESTING=True)
        app_v2.legacy.ALLOW_TEST_AUTH = True

    def setUp(self):
        app_v2.legacy.db.session.remove()
        app_v2.legacy.Model.metadata.drop_all(app_v2.legacy.engine)
        app_v2.legacy.Model.metadata.create_all(app_v2.legacy.engine)
        app_v2.legacy.ADMIN_TELEGRAM_IDS.clear()
        self.first = app_v2.app.test_client()
        self.second = app_v2.app.test_client()
        self.guest = app_v2.app.test_client()

    def login(self, client, number):
        response = client.post('/auth/test', json={'user': str(number)})
        self.assertEqual(response.status_code, 200, response.get_json())

    def profile(self, client, name, suffix):
        response = client.post('/api/profile', json={
            'name': name,
            'age': 40,
            'gender': 'male' if name != 'Анна' else 'female',
            'about': 'Люблю городские прогулки и полезные совместные дела.',
            'selfie': f'data:image/jpeg;base64,selfie-{suffix}',
            'public_image_kind': 'avatar',
            'public_image': f'data:image/jpeg;base64,avatar-{suffix}',
            'real_photo': f'data:image/jpeg;base64,real-{suffix}',
            'terms_accepted': True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_guest_feed_hides_people_and_registered_feed_uses_zone(self):
        self.login(self.first, 1)
        self.profile(self.first, 'Юрий', 'one')
        point = {'latitude': 53.9023, 'longitude': 27.5619}
        self.first.post('/api/location', json=point)
        self.first.post('/api/presence', json={**point, 'category': 'cafe'})

        guest = self.guest.get('/api/feed?lat=53.906&lon=27.568&radius=3&category=cafe')
        self.assertEqual(guest.status_code, 200)
        self.assertEqual(guest.get_json()['items'], [])
        self.assertEqual(guest.get_json()['total_activity'], 1)

        self.login(self.second, 2)
        self.profile(self.second, 'Анна', 'two')
        feed = self.second.get('/api/feed?lat=53.906&lon=27.568&radius=3&category=cafe').get_json()['items']
        self.assertEqual(len(feed), 1)
        self.assertTrue(feed[0]['approximate_zone'])
        self.assertEqual(feed[0]['zone_radius_m'], 500)
        self.assertNotEqual(feed[0]['latitude'], point['latitude'])
        self.assertEqual(feed[0]['verification_label'], 'Селфи-проверка пройдена')
        self.assertIn('distance_band', feed[0])

    def test_selfie_is_never_used_as_meeting_photo(self):
        self.login(self.first, 11)
        self.login(self.second, 12)
        self.profile(self.first, 'Юрий', 'first')
        self.profile(self.second, 'Анна', 'second')
        point = {'latitude': 53.9023, 'longitude': 27.5619}
        created = self.first.post('/api/meetings', json={
            **point, 'category': 'walk', 'description': 'Прогуляться вместе', 'format': 'one',
            'time_mode': 'now', 'starts_in_minutes': 0,
        })
        meeting_id = created.get_json()['id']
        self.second.post(f'/api/meetings/{meeting_id}/interest', json={})
        interest = self.first.get('/api/interests').get_json()['incoming'][0]
        self.first.post(f"/api/interests/{interest['id']}/decision", json={'decision': 'accepted'})

        room = self.second.get(f'/api/meetings/{meeting_id}/room').get_json()
        other = next(item for item in room['participants'] if not item['mine'])
        self.assertIsNone(other['picture'])
        self.assertIn('avatar-first', other['public_picture'])
        self.assertNotIn('selfie-first', str(room))

        self.second.post(f'/api/meetings/{meeting_id}/photo-consent', json={})
        revealed = self.first.post(f'/api/meetings/{meeting_id}/photo-consent', json={}).get_json()['room']
        pictures = {item['picture'] for item in revealed['participants']}
        self.assertTrue(any('real-first' in value for value in pictures))
        self.assertTrue(any('real-second' in value for value in pictures))
        self.assertFalse(any('selfie-' in value for value in pictures))

    def test_invalid_delete_confirmation_keeps_v2_data(self):
        self.login(self.first, 15)
        self.profile(self.first, 'Юрий', 'delete')
        media_count = app_v2.v2.ProfileMedia.query.count()
        response = self.first.delete('/api/account', json={'confirmation': 'нет'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(app_v2.v2.ProfileMedia.query.count(), media_count)
        self.assertEqual(app_v2.legacy.User.query.count(), 1)

    def test_good_deed_moderation_and_confirmed_trace(self):
        self.login(self.first, 21)
        self.login(self.second, 22)
        self.profile(self.first, 'Юрий', 'owner')
        self.profile(self.second, 'Анна', 'member')

        starts = (app_v2.legacy.utcnow() + timedelta(hours=2)).isoformat()
        deed_response = self.first.post('/api/v2/good-deeds', json={
            'title': 'Помочь приюту собрать корм',
            'description': 'Собираем и сортируем корм вместе с координатором приюта.',
            'organizer_name': 'Городской приют',
            'coordinator_name': 'Мария',
            'area': 'Публичная площадка у входа',
            'starts_at': starts,
            'duration_minutes': 120,
            'capacity': 12,
            'instructions': 'Взять перчатки. Деньги, документы и доступы не требуются.',
        })
        self.assertEqual(deed_response.status_code, 201, deed_response.get_json())
        self.assertTrue(deed_response.get_json()['pending'])
        deed_id = deed_response.get_json()['item']['id']
        self.assertEqual(self.guest.get('/api/v2/good-deeds').get_json()['items'], [])

        app_v2.legacy.ADMIN_TELEGRAM_IDS.add('test-21')
        approved = self.first.post(f'/api/v2/admin/good-deeds/{deed_id}/decision', json={'decision': 'active'})
        self.assertEqual(approved.status_code, 200, approved.get_json())
        self.assertEqual(approved.get_json()['item']['status'], 'active')

        joined = self.second.post(f'/api/v2/good-deeds/{deed_id}/join', json={})
        self.assertEqual(joined.status_code, 200, joined.get_json())
        completed = self.first.post(f'/api/v2/good-deeds/{deed_id}/complete', json={
            'result_summary': 'Собрано и отсортировано 80 килограммов корма.',
            'hours': 2,
        })
        self.assertEqual(completed.status_code, 200, completed.get_json())
        trace = self.second.get('/api/v2/good-trace').get_json()
        self.assertEqual(trace['confirmed_deeds'], 1)
        self.assertEqual(trace['hours'], 2.0)


if __name__ == '__main__':
    unittest.main()
