"""Synthetic desktop tests: no real Viber connections or messages."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from app.accounts import stamp
from app.storage import connect
from app.web_service import WebService
import test_controller


class ZeroWaitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_controller.DraftTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.fixture.replies.configure(True, self.fixture.replies.settings()['instructions'], require_approval=False)

    def tearDown(self):
        self.fixture.tearDown()

    def campaign(self):
        text = 'phone,company_name,outreach_message\n' + ''.join(
            f'064{i:07d},Business {i},Outreach {i}\n' for i in range(3))
        imported = self.service.csv_import.import_leads(text)
        draft = self.service.campaigns.create({
            'request_key': str(uuid.uuid4()), 'name': 'Zero wait simulation', 'lead_ids': imported['lead_ids'],
            'text': '', 'schedule': {'mode': 'delay', 'minutes': 60}, 'interval_minutes': 0,
            'daily_cap': 100, 'window_start': '00:00', 'window_end': '23:59'})
        preview = self.service.campaigns.review(draft['id'])
        active = self.service.campaigns.activate({'preview_token': preview['token'], 'confirmed': True, 'request_key': str(uuid.uuid4())})
        with closing(connect(self.service.store.path)) as db, db:
            # An hour of queued backlog must not become a MISSED fixed-time send.
            db.execute('UPDATE web_sends SET scheduled_at=? WHERE campaign_id=?', (stamp(-3600), active['id']))
        return active['id']

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Synthetic worker did not reach its expected state.')

    def test_ready_codex_reply_interrupts_waiting_outreach_then_queue_resumes(self):
        campaign_id = self.campaign()
        first_entered, release_first = threading.Event(), threading.Event()
        original_open = self.fixture.desktop.open_phone

        def open_phone(phone):
            if not first_entered.is_set():
                first_entered.set()
                if not release_first.wait(10):
                    raise ValueError('Test failed to release its first desktop action.')
            return original_open(phone)

        self.fixture.desktop.open_phone = open_phone
        self.service.start_scheduler()
        try:
            self.assertTrue(first_entered.wait(3))
            self.fixture.incoming()
            self.fixture.replies.tick()
            self.fixture.wait_state('QUEUED')
            self.assertEqual(self.fixture.desktop.sent, [])
        finally:
            release_first.set()
        self.wait_for(lambda: self.service.campaigns.detail(campaign_id)['state'] == 'COMPLETED')
        self.assertEqual(self.fixture.desktop.sent, ['Outreach 0', 'Zdravo!', 'Outreach 1', 'Outreach 2'])
        self.assertTrue(self.fixture.replies.settings()['enabled'])
        self.assertFalse(self.fixture.replies.settings()['review_required'])
        with closing(connect(self.service.store.path)) as db:
            jobs = list(db.execute('SELECT kind,priority FROM controller_jobs ORDER BY started_at'))
            self.assertEqual([(r['kind'], r['priority']) for r in jobs], [
                ('scheduled-send', 10), ('automatic-reply', 100), ('scheduled-send', 10), ('scheduled-send', 10)])
            self.assertEqual(db.execute("SELECT count(*) FROM web_sends WHERE campaign_id=? AND state='DISPATCHED'", (campaign_id,)).fetchone()[0], 3)

    def test_zero_wait_backlog_survives_restart_without_replaying_completed_sends(self):
        campaign_id = self.campaign()
        task = self.service._dispatch_due()
        self.assertIsNotNone(task)
        self.wait_for(lambda: not self.service.pending)
        self.assertEqual(self.fixture.desktop.sent, ['Outreach 0'])
        self.service.close()
        self.service = self.fixture.service = WebService(self.fixture.path, lambda: self.fixture.desktop,
                                                       lambda *_: None, managed=True)
        self.service.watcher.source = self.fixture.source
        self.service.start_scheduler()
        self.wait_for(lambda: self.service.campaigns.detail(campaign_id)['state'] == 'COMPLETED')
        self.assertEqual(self.fixture.desktop.sent, ['Outreach 0', 'Outreach 1', 'Outreach 2'])
        self.assertTrue(self.service.replies.settings()['enabled'])

    def test_uncertain_zero_wait_send_pauses_remaining_without_retry(self):
        campaign_id = self.campaign()
        self.fixture.desktop.fail = True
        self.service.start_scheduler()
        self.wait_for(lambda: self.service.campaigns.detail(campaign_id)['state'] == 'PAUSED')
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(self.fixture.desktop.sent, ['Outreach 0'])
        with closing(connect(self.service.store.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM web_sends WHERE campaign_id=? AND state='UNKNOWN'", (campaign_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM web_sends WHERE campaign_id=? AND attempted_at IS NOT NULL", (campaign_id,)).fetchone()[0], 1)

    def test_zero_wait_respects_daily_limit_before_next_desktop_action(self):
        campaign_id = self.campaign()
        with closing(connect(self.service.store.path)) as db, db:
            rules = json.loads(db.execute('SELECT rules FROM web_campaigns WHERE id=?', (campaign_id,)).fetchone()[0])
            rules['daily_cap'] = 1
            db.execute('UPDATE web_campaigns SET rules=? WHERE id=?', (json.dumps(rules), campaign_id))
        task = self.service._dispatch_due()
        self.assertIsNotNone(task)
        self.wait_for(lambda: not self.service.pending)
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(self.fixture.desktop.sent, ['Outreach 0'])
        self.assertEqual(self.service.campaigns.detail(campaign_id)['state'], 'ACTIVE')

    def test_daily_limit_accepts_1000_and_higher_without_an_artificial_cap(self):
        for cap in (1000, 10000):
            with self.subTest(cap=cap):
                leads = self.service.csv_import.import_leads(f'phone,company_name\n064{cap:07d},Business {cap}\n')
                draft = self.service.campaigns.create({
                    'request_key': str(uuid.uuid4()), 'name': f'Cap {cap}', 'lead_ids': leads['lead_ids'],
                    'text': 'Synthetic outreach', 'schedule': {'mode': 'delay', 'minutes': 60}, 'interval_minutes': 0,
                    'daily_cap': cap, 'window_start': '09:00', 'window_end': '17:00'})
                self.assertEqual(draft['rules']['daily_cap'], cap)

    def test_codex_sends_at_2am_while_daytime_outreach_remains_scheduled(self):
        campaign_id = self.campaign()
        night = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)  # 02:00 Europe/Warsaw.

        class NightClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return night.astimezone(tz) if tz else night.replace(tzinfo=None)

        with closing(connect(self.service.store.path)) as db, db:
            rules = json.loads(db.execute('SELECT rules FROM web_campaigns WHERE id=?', (campaign_id,)).fetchone()[0])
            rules.update(window_start='09:00', window_end='17:00', daily_cap=1000)
            db.execute('UPDATE web_campaigns SET rules=? WHERE id=?', (json.dumps(rules), campaign_id))
            db.execute('UPDATE web_sends SET scheduled_at=? WHERE campaign_id=?', ((night-timedelta(hours=1)).isoformat(), campaign_id))
        with patch('app.web_service.datetime', NightClock):
            self.assertIsNone(self.service._dispatch_due())
            self.fixture.incoming()
            self.fixture.replies.tick()
            self.fixture.wait_state('DISPATCHED')
        self.assertEqual(self.fixture.desktop.sent, ['Zdravo!'])
        self.assertEqual(self.service.campaigns.detail(campaign_id)['counts'], {'SCHEDULED': 3})

    def test_codex_sends_after_daily_outreach_allowance_is_used(self):
        campaign_id = self.campaign()
        with closing(connect(self.service.store.path)) as db, db:
            rules = json.loads(db.execute('SELECT rules FROM web_campaigns WHERE id=?', (campaign_id,)).fetchone()[0])
            rules['daily_cap'] = 1
            db.execute('UPDATE web_campaigns SET rules=? WHERE id=?', (json.dumps(rules), campaign_id))
        self.assertIsNotNone(self.service._dispatch_due())
        self.wait_for(lambda: not self.service.pending)
        self.assertIsNone(self.service._dispatch_due())
        self.fixture.incoming()
        self.fixture.replies.tick()
        self.fixture.wait_state('DISPATCHED')
        self.assertEqual(self.fixture.desktop.sent, ['Outreach 0', 'Zdravo!'])
        with closing(connect(self.service.store.path)) as db:
            used, _ = self.service.campaigns._usage(db, campaign_id)
            self.assertEqual(sum(used.values()), 1)
