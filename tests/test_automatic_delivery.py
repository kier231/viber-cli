"""Managed automatic delivery keeps account isolation and durable-send safeguards."""
from contextlib import closing
import json
import threading
import time
import unittest
from unittest.mock import Mock

from app.accounts import stamp
from app.storage import connect
from app.web_service import WebService
import test_controller
from test_reply_feedback import ASSUMPTION
from test_auto_replies import Source


class AutomaticDeliveryTests(unittest.TestCase):
    tearDown = test_controller.DraftTests.tearDown
    incoming = test_controller.DraftTests.incoming
    wait_state = test_controller.DraftTests.wait_state

    def setUp(self):
        test_controller.DraftTests.setUp(self)
        self.replies.configure(True, self.replies.settings()['instructions'], require_approval=False)

    def test_generated_reply_sends_once_without_approval_and_keeps_durable_metadata(self):
        self.incoming(); self.replies.tick()
        reply = self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent, ['Zdravo!'])
        self.assertFalse(self.replies.settings()['review_required'])
        self.assertEqual(self.replies.settings()['generation_slots'], 2)
        with closing(connect(self.path)) as db:
            job = db.execute('SELECT * FROM auto_reply_jobs WHERE id=?', (reply['id'],)).fetchone()
            queued = db.execute('SELECT * FROM controller_jobs WHERE operation_id=?', (job['operation_id'],)).fetchone()
            self.assertIsNone(job['approved_at'])
            self.assertEqual(job['review_ms'], 0)
            self.assertIsNotNone(job['queued_at'])
            self.assertEqual((queued['priority'], queued['phone']), (100, reply['phone']))
            self.assertEqual(json.loads(queued['payload']), {'reply_job_id': reply['id']})
        self.replies.tick()
        self.assertEqual(len(self.generator.contexts), 1)
        self.assertEqual(len(self.desktop.sent), 1)

    def test_incoming_domain_link_reaches_codex_and_replies_once(self):
        from test_viber_inbox import message
        self.source.data['messages'].append(message(2, body='bridgesolver.com', message_type=9))
        self.source.data['max_event_id'] = 2
        self.service.watcher.poll_once()
        self.replies.tick(); self.wait_state('DISPATCHED')
        self.assertEqual(self.generator.contexts[0]['messages'][-1]['body'], 'bridgesolver.com')
        self.assertEqual(self.generator.contexts[0]['messages'][-1]['message_type'], 9)
        self.replies.tick()
        self.assertEqual(len(self.generator.contexts), 1)
        self.assertEqual(self.desktop.sent, ['Zdravo!'])

    def test_assumption_question_is_private_and_does_not_delay_send(self):
        self.generator.result = {'action':'reply', 'text':'Mogu da predlozim obim.', 'reason':'Proposal', 'assumptions':[ASSUMPTION]}
        self.incoming(); self.replies.tick()
        self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent, ['Mogu da predlozim obim.'])
        self.assertEqual(self.replies.feedback.questions()[0]['state'], 'OPEN')
        self.assertNotIn(ASSUMPTION['question'], self.desktop.sent[0])

    def test_reply_uses_verified_number_even_when_saved_name_differs(self):
        self.service.store.set_viber_name(1,'Mihajlo')
        live = Mock(wraps=self.desktop)
        live.connect.return_value = live
        live.open_phone.return_value = 'Brt'
        live.verify_current_name.side_effect = lambda name: name == 'Brt'
        self.service.client_factory = lambda: live
        self.incoming(); self.replies.tick()
        reply = self.wait_state('DISPATCHED')
        live.open_phone.assert_called_once_with(reply['phone'])
        self.assertEqual(self.desktop.sent,['Zdravo!'])
        self.assertEqual(self.service.records('sent')[0]['viber_name'],'Brt')

    def test_reply_with_unstable_live_chat_waits_without_sending(self):
        live = Mock(wraps=self.desktop)
        live.connect.return_value = live
        live.open_phone.return_value = 'Brt'
        live.verify_current_name.return_value = False
        self.service.client_factory = lambda: live
        self.incoming(); self.replies.tick()
        self.wait_state('RETRY')
        self.assertEqual(self.desktop.sent,[])

    def test_hold_and_invalid_generation_never_send_or_retry(self):
        for result in ({'action':'hold','text':'','reason':'No reply needed'},
                       {'action':'reply','text':'bad\ntext','reason':'Invalid'}):
            self.generator.result = result
            event = 2 + len(self.generator.contexts)
            self.incoming(event); self.replies.tick()
            self.wait_state('HELD')
            self.replies.tick()
        self.assertEqual(self.desktop.sent, [])
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM controller_jobs WHERE kind='automatic-reply'").fetchone()[0], 0)

    def test_pause_during_generation_cancels_automatic_send(self):
        self.generator.callback = lambda: self.replies.configure(False, self.replies.settings()['instructions'])
        self.incoming(); self.replies.tick()
        self.wait_state('HELD')
        self.assertEqual(self.desktop.sent, [])

    def test_new_message_during_generation_sends_only_fresh_result(self):
        self.generator.callback = lambda: self.incoming(3)
        self.incoming(); self.replies.tick()
        self.wait_state('REGENERATE')
        self.assertEqual(self.desktop.sent, [])
        self.generator.callback = None
        self.replies.tick(); self.wait_state('DISPATCHED')
        self.assertEqual(len(self.desktop.sent), 1)
        self.assertEqual(len(self.generator.contexts), 2)

    def cancelled_after_history_change(self):
        def remove_earlier_message():
            self.source.data['messages'] = [m for m in self.source.data['messages'] if m['event_id'] != 1]
            self.service.watcher.poll_once()
        self.generator.callback = remove_earlier_message
        self.incoming(); self.replies.tick()
        cancelled = self.wait_state('REGENERATE')
        self.generator.callback = None
        deadline = time.monotonic() + 3
        while self.replies.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.desktop.sent, [])
        return cancelled

    def test_deleted_history_rebuilds_unsent_reply_once_with_current_context(self):
        cancelled = self.cancelled_after_history_change()
        self.generator.result = {'action':'reply','text':'Fresh reply','reason':'Current history'}
        self.replies.tick()
        sent = self.wait_state('DISPATCHED')
        self.assertEqual(sent['id'], cancelled['id'])
        self.assertEqual(self.desktop.sent, ['Fresh reply'])
        self.assertEqual(len(self.generator.contexts), 2)
        self.assertEqual([m['event_id'] for m in self.generator.contexts[-1]['messages']], [2])
        self.assertEqual([m['event_id'] for m in self.generator.contexts[-1]['retained_background']], [1])
        self.replies.tick()
        self.assertEqual(len(self.desktop.sent), 1)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_reply_jobs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_reply_events').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_sends').fetchone()[0], 1)

    def test_cancelled_generation_does_not_rebuild_after_manual_reply(self):
        self.cancelled_after_history_change()
        from test_viber_inbox import message
        self.source.data['messages'].append(message(3,'OUTGOING',body='Manual answer'))
        self.source.data['max_event_id'] = 3
        self.service.watcher.poll_once()
        self.replies.tick()
        self.wait_state('STALE')
        self.assertEqual(len(self.generator.contexts), 1)
        self.assertEqual(self.desktop.sent, [])

    def test_cancelled_generation_preserves_explicit_pause_and_instruction_change(self):
        self.cancelled_after_history_change()
        instructions = self.replies.settings()['instructions']
        self.replies.configure(False, instructions)
        self.replies.tick()
        self.assertEqual(len(self.generator.contexts), 1)
        self.replies.configure(True, instructions + '\nNew owner instruction')
        self.replies.tick()
        self.wait_state('STALE')
        self.assertEqual(len(self.generator.contexts), 1)
        self.assertEqual(self.desktop.sent, [])

    def test_cancelled_generation_never_rebuilds_with_uncertain_send(self):
        cancelled = self.cancelled_after_history_change()
        with closing(connect(self.path)) as db, db:
            db.execute('''INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,
                phone,company_name,viber_name,text,state,created_at,updated_at)
                VALUES('uncertain','manual:uncertain','hash','other',1,?,'Business','Person',
                'Possible answer','UNKNOWN',?,?)''',
                (cancelled['phone'], stamp(), stamp()))
        self.replies.tick()
        self.wait_state('STALE')
        self.assertEqual(len(self.generator.contexts), 1)
        self.assertEqual(self.desktop.sent, [])

    def test_contact_change_while_queued_blocks_automatic_send(self):
        gate = threading.Event()
        entered = threading.Event()
        def blocked():
            entered.set(); gate.wait(5)
        self.service._enqueue('unrelated', blocked)
        try:
            self.assertTrue(entered.wait(2))
            self.incoming(); self.replies.tick()
            self.wait_state('QUEUED')
            self.service.store.set_viber_name(1, 'Changed recipient')
        finally:
            gate.set()
        self.wait_state('BLOCKED')
        self.assertEqual(self.desktop.sent, [])

    def test_ownership_change_during_generation_blocks_automatic_send(self):
        def change():
            with closing(connect(self.path)) as db, db:
                db.execute("INSERT INTO accounts(id,phone,enabled,created_at) VALUES('other','+381641111112',0,?)", (stamp(),))
                db.execute("UPDATE contact_owners SET account_id='other' WHERE phone='+381641234567'")
        self.generator.callback = change
        self.incoming(); self.replies.tick()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with closing(connect(self.path)) as db:
                job = db.execute('SELECT state FROM auto_reply_jobs').fetchone()
                if job and job[0] == 'HELD':
                    break
            time.sleep(.01)
        else:
            self.fail('Foreign ownership did not stop generation')
        self.assertEqual(self.desktop.sent, [])

    def test_uncertain_send_pauses_chat_and_is_never_retried(self):
        self.desktop.fail = True
        self.incoming(); self.replies.tick()
        self.wait_state('UNKNOWN')
        self.assertEqual(len(self.desktop.sent), 1)
        self.assertFalse(self.service.watcher.inbox.conversations()[0]['reply_enabled'])
        self.incoming(3); self.replies.tick()
        self.assertEqual(len(self.desktop.sent), 1)
        self.assertEqual(len(self.replies.jobs()), 1)

    def test_switching_to_automatic_does_not_send_existing_drafts(self):
        self.replies.configure(True, self.replies.settings()['instructions'], require_approval=True)
        self.incoming(); self.replies.tick()
        draft = self.wait_state('DRAFT')
        self.replies.configure(True, self.replies.settings()['instructions'], require_approval=False)
        self.wait_state('STALE')
        self.replies.tick()
        with self.assertRaises(ValueError):
            self.replies.review_draft(draft['id'], True)
        self.assertEqual(self.desktop.sent, [])
        self.replies.regenerate(draft['id'])
        self.wait_state('DISPATCHED')
        self.assertEqual(len(self.desktop.sent), 1)

    def test_queued_automatic_reply_resumes_once_after_confirmed_worker_shutdown(self):
        gate = threading.Event()
        entered = threading.Event()
        def blocked():
            entered.set(); gate.wait(3)
        self.service._enqueue('unrelated', blocked)
        self.assertTrue(entered.wait(2))
        self.incoming(); self.replies.tick()
        queued = self.wait_state('QUEUED')
        # Stop the queue before releasing the unrelated operation, leaving the
        # reply durably pending rather than interrupting any send action.
        self.service.pool.stop.set()
        gate.set()
        self.service.close()
        self.assertEqual(self.desktop.sent, [])
        self.service = WebService(self.path, lambda:self.desktop, lambda *_:None,
                                  source_factory=lambda:Source(self.source.data), managed=True)
        self.replies = self.service.replies
        self.wait_state('DISPATCHED')
        self.assertEqual(len(self.desktop.sent), 1)
        self.assertFalse(self.replies.settings()['review_required'])
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_sends WHERE request_key=?', ('auto:' + queued['id'],)).fetchone()[0], 1)

    def test_automatic_mode_api_requires_authentication_and_boolean(self):
        from fastapi.testclient import TestClient
        from app.controller import create_app
        client = TestClient(create_app(self.service), base_url='http://127.0.0.1:4001')
        session = client.post('/viber/api/session',json={},headers={'Origin':'http://127.0.0.1:4001','X-Viber-Browser':'1'})
        headers = {'Origin':'http://127.0.0.1:4001','X-Viber-CSRF':session.json()['csrf']}
        payload = {'enabled':True,'instructions':self.replies.settings()['instructions'],'require_approval':False}
        self.assertEqual(client.post('/viber/api/replies',json=payload).status_code,403)
        self.assertEqual(client.post('/viber/api/replies',json={**payload,'require_approval':0},headers=headers).status_code,400)
        response = client.post('/viber/api/replies',json=payload,headers=headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['delivery_mode'],'automatic')
