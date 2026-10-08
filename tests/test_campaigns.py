from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from zoneinfo import ZoneInfo

from app.campaigns import campaign_slots
from app.web_service import WebService
from test_web_service import FakeDesktop


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'campaigns.sqlite3'
        FakeDesktop.name, FakeDesktop.fail_send = 'Viber Person', False
        FakeDesktop.sent.clear()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.leads = [self.service.store.create_with_android(f'+38164{index:07d}', f'Business {index}', lambda *_: None) for index in range(3)]

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def payload(self, **changes):
        return {'request_key': str(uuid.uuid4()), 'name': 'October outreach', 'lead_ids': [lead.id for lead in self.leads],
                'text': 'Zdravo {{name}}, {{company}}!', 'schedule': {'mode': 'delay', 'minutes': 60},
                'interval_minutes': 5, 'daily_cap': 100, 'window_start': '00:00', 'window_end': '23:59', **changes}

    def activate(self, draft=None):
        draft = draft or self.service.campaigns.create(self.payload())
        review = self.service.campaigns.review(draft['id'])
        payload = {'preview_token': review['token'], 'confirmed': True, 'request_key': str(uuid.uuid4())}
        return self.service.campaigns.activate(payload), payload

    def due(self, send_id, minutes=0):
        value = (datetime.now(timezone.utc) - timedelta(minutes=minutes, seconds=1)).isoformat(timespec='seconds')
        with closing(self.service.store._connect()) as db, db:
            db.execute('UPDATE web_sends SET scheduled_at=? WHERE id=?', (value, send_id))

    def dispatch(self):
        task = self.service._dispatch_due()
        self.assertIsNotNone(task)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.service.operation(task['operation_id'])
            if result['state'] not in {'QUEUED', 'RUNNING'} and not self.service.pending:
                return result
            time.sleep(.01)
        self.fail('Campaign operation did not finish')

    def test_100_unique_recipients_are_drafts_until_reviewed_confirmation(self):
        for index in range(3, 100):
            self.leads.append(self.service.store.create_with_android(f'+38164{index:07d}', f'Business {index}', lambda *_: None))
        draft = self.service.campaigns.create(self.payload())
        self.assertEqual(draft['counts'], {'DRAFT': 100})
        self.assertEqual(draft['recipients'][0]['text'], 'Zdravo Business 0, Business 0!')
        self.assertEqual(self.service.records('scheduled'), [])
        self.assertIsNone(self.service._dispatch_due())
        review = self.service.campaigns.review(draft['id'])
        with self.assertRaises(ValueError):
            self.service.campaigns.activate({'preview_token': review['token'], 'confirmed': False, 'request_key': str(uuid.uuid4())})
        active, _ = self.activate(draft)
        self.assertEqual(active['counts'], {'SCHEDULED': 100})
        self.assertEqual(FakeDesktop.sent, [])

    def test_duplicate_phone_rows_receive_one_message(self):
        duplicate = self.service.store.create_with_android(self.leads[0].phone, 'Duplicate business', lambda *_: None)
        draft = self.service.campaigns.create(self.payload(lead_ids=[self.leads[0].id, duplicate.id, self.leads[1].id]))
        self.assertEqual(draft['total'], 2)
        self.assertEqual(draft['duplicates_skipped'], 1)
        self.assertEqual(len(self.service.contacts()), 4)

    def test_create_and_activation_are_idempotent_after_restart(self):
        payload = self.payload()
        draft = self.service.campaigns.create(payload)
        self.assertEqual(self.service.campaigns.create(payload)['id'], draft['id'])
        active, confirmation = self.activate(draft)
        self.service.close()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.assertEqual(self.service.campaigns.create(payload)['id'], active['id'])
        self.assertEqual(self.service.campaigns.activate(confirmation)['id'], active['id'])
        self.assertEqual(len(self.service.campaigns.list()), 1)
        self.assertEqual(active['total'], 3)
        with self.assertRaisesRegex(ValueError, 'another campaign'):
            self.service.campaigns.create({**payload, 'text': 'Other message'})

    def test_invalid_batch_is_atomic(self):
        for changes in ({'lead_ids': []}, {'lead_ids': [True]}, {'lead_ids': [1] * 101}, {'interval_minutes': 0},
                        {'daily_cap': 101}, {'window_end': '00:00'}, {'text': 'Hi {{invalid}}'}, {'text': 'Hi {{viber_name}}'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.service.campaigns.create(self.payload(**changes))
        self.assertEqual(self.service.campaigns.list(), [])
        self.assertEqual(FakeDesktop.sent, [])

    def test_changed_contact_and_expired_review_reject_activation(self):
        draft = self.service.campaigns.create(self.payload())
        review = self.service.campaigns.review(draft['id'])
        payload = {'preview_token': review['token'], 'confirmed': True, 'request_key': str(uuid.uuid4())}
        self.service.store.set_viber_name(self.leads[0].id, 'New person')
        with self.assertRaisesRegex(ValueError, 'contact changed'):
            self.service.campaigns.activate(payload)
        self.assertEqual(self.service.campaigns.detail(draft['id'])['counts'], {'DRAFT': 3})
        review = self.service.campaigns.review(draft['id'])
        self.service.campaigns.previews[review['token']]['expires_at'] = 0
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.service.campaigns.activate({**payload, 'preview_token': review['token']})

    def test_send_learns_name_preserves_business_and_spaces_actual_attempts(self):
        active, _ = self.activate()
        first, second = active['recipients'][:2]
        self.due(first['id'])
        self.due(second['id'])
        result = self.dispatch()
        self.assertEqual(result['state'], 'SUCCEEDED', result['error'])
        detail = self.service.campaigns.detail(active['id'])
        self.assertEqual(detail['counts'], {'DISPATCHED': 1, 'SCHEDULED': 2})
        self.assertEqual(FakeDesktop.sent, [first['text']])
        lead = self.service.store.get(first['lead_id'])
        self.assertEqual(lead.company_name, 'Business 0')
        self.assertEqual(lead.contact_name, 'Business 0 | SJT-1')
        self.assertEqual(lead.viber_name, 'Viber Person')
        self.assertGreater(datetime.fromisoformat(detail['next_at']).timestamp(), time.time() + 290)
        self.due(second['id'])
        self.assertIsNone(self.service._dispatch_due())  # Actual spacing prevents catch-up bursts.

    def test_pause_cancel_and_resume_only_remaining(self):
        active, _ = self.activate()
        self.service.campaigns.control(active['id'], 'pause')
        self.due(active['recipients'][0]['id'])
        self.assertIsNone(self.service._dispatch_due())
        resumed, _ = self.activate(active)
        self.assertEqual(resumed['state'], 'ACTIVE')
        cancelled = self.service.campaigns.control(active['id'], 'cancel')
        self.assertEqual(cancelled['counts'], {'CANCELLED': 3})
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(FakeDesktop.sent, [])

    def test_unknown_pauses_and_resume_does_not_retry_uncertain_message(self):
        active, _ = self.activate()
        self.due(active['recipients'][0]['id'])
        FakeDesktop.fail_send = True
        self.assertEqual(self.dispatch()['state'], 'FAILED')
        paused = self.service.campaigns.detail(active['id'])
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['counts'], {'SCHEDULED': 2, 'UNKNOWN': 1})
        FakeDesktop.fail_send = False
        resumed, _ = self.activate(paused)
        self.assertEqual(resumed['counts'], {'SCHEDULED': 2, 'UNKNOWN': 1})
        self.service._dispatch_due()
        self.assertEqual(self.service.campaigns.detail(active['id'])['state'], 'ACTIVE')
        self.assertEqual(len(FakeDesktop.sent), 1)

    def test_missed_first_message_pauses_remaining_batch(self):
        active, _ = self.activate()
        self.due(active['recipients'][0]['id'], 16)
        self.due(active['recipients'][1]['id'])
        self.assertIsNone(self.service._dispatch_due())
        detail = self.service.campaigns.detail(active['id'])
        self.assertEqual(detail['state'], 'PAUSED')
        self.assertEqual(detail['counts'], {'MISSED': 1, 'SCHEDULED': 2})
        self.assertEqual(FakeDesktop.sent, [])

    def test_known_name_mismatch_blocks_and_pauses(self):
        self.service.store.set_viber_name(self.leads[0].id, 'Expected person')
        active, _ = self.activate()
        self.due(active['recipients'][0]['id'])
        self.assertEqual(self.dispatch()['state'], 'FAILED')
        self.assertEqual(self.service.campaigns.detail(active['id'])['state'], 'PAUSED')
        self.assertEqual(FakeDesktop.sent, [])

    def test_restart_with_interrupted_campaign_pauses_without_retry(self):
        active, _ = self.activate()
        with closing(self.service.store._connect()) as db, db:
            db.execute("UPDATE web_sends SET state='SUBMITTING' WHERE id=?", (active['recipients'][0]['id'],))
        self.service.close()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.assertEqual(self.service.campaigns.detail(active['id'])['state'], 'PAUSED')
        self.assertEqual(FakeDesktop.sent, [])

    def test_resume_waits_for_claimed_action_to_settle(self):
        active, _ = self.activate()
        with closing(self.service.store._connect()) as db, db:
            db.execute("UPDATE web_sends SET state='QUEUED' WHERE id=?", (active['recipients'][0]['id'],))
        self.service.campaigns.control(active['id'], 'pause')
        with self.assertRaisesRegex(ValueError, 'active desktop action'):
            self.service.campaigns.review(active['id'])
        self.assertEqual(self.service.records('scheduled')[0]['campaign_state'], 'PAUSED')

    def test_complete_and_cancel_after_claim_before_send(self):
        draft = self.service.campaigns.create(self.payload(lead_ids=[self.leads[0].id]))
        active, _ = self.activate(draft)
        first = active['recipients'][0]
        self.due(first['id'])
        self.assertEqual(self.dispatch()['state'], 'SUCCEEDED')
        self.assertEqual(self.service.campaigns.detail(active['id'])['state'], 'COMPLETED')
        another, _ = self.activate()
        send = another['recipients'][0]
        with closing(self.service.store._connect()) as db, db:
            db.execute("UPDATE web_sends SET state='QUEUED' WHERE id=?", (send['id'],))
        self.service.campaigns.control(another['id'], 'cancel')
        result = self.service._send(send['id'], {'campaign_id': another['id']})
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertEqual(len(FakeDesktop.sent), 1)


class CampaignPlanningTests(unittest.TestCase):
    def test_100_slots_obey_daily_cap_window_and_dst(self):
        rules = {'interval_minutes': 5, 'daily_cap': 40, 'window_start': '09:00', 'window_end': '17:00'}
        slots = campaign_slots('2026-10-24T06:00:00+00:00', 100, rules)
        utc_slots = [datetime.fromisoformat(value) for value in slots]
        local_slots = [value.astimezone(ZoneInfo('Europe/Warsaw')) for value in utc_slots]
        self.assertEqual(list(Counter(str(value.date()) for value in local_slots).values()), [40, 40, 20])
        self.assertTrue(all(9 <= value.hour < 17 for value in local_slots))
        self.assertTrue(all(b - a >= timedelta(minutes=5) for a, b in zip(utc_slots, utc_slots[1:])))
        self.assertEqual(utc_slots[0].hour, 7)
        self.assertEqual(utc_slots[40].hour, 8)

    def test_actual_attempt_usage_moves_remaining_to_next_day(self):
        rules = {'interval_minutes': 10, 'daily_cap': 1, 'window_start': '09:00', 'window_end': '17:00'}
        slots = campaign_slots('2026-10-08T08:00:00+00:00', 2, rules, {'2026-10-08': 1}, '2026-10-08T07:59:00+00:00')
        self.assertEqual(slots, ['2026-10-09T07:00:00+00:00', '2026-10-10T07:00:00+00:00'])


if __name__ == '__main__':
    unittest.main()
