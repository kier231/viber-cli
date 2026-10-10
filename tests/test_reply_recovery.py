"""An incoming turn survives temporary failures without a duplicate send."""
from contextlib import closing
import time
import unittest
from unittest.mock import Mock

from app.auto_replies import AutoReplies
from app.reply_errors import ReplyRetryable, ViberRetryable
from app.storage import connect
import test_automatic_delivery as delivery


class ReplyRecoveryTests(unittest.TestCase):
    setUp = delivery.AutomaticDeliveryTests.setUp
    tearDown = delivery.AutomaticDeliveryTests.tearDown
    incoming = delivery.AutomaticDeliveryTests.incoming
    wait_state = delivery.AutomaticDeliveryTests.wait_state

    def due(self):
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            with closing(connect(self.path)) as db, db:
                busy = db.execute("SELECT 1 FROM controller_jobs WHERE state='RUNNING'").fetchone()
                if not busy and not self.replies.active:
                    db.execute("UPDATE auto_reply_jobs SET retry_at='2000-01-01T00:00:00+00:00' WHERE state='RETRY'")
                    return
            time.sleep(.01)
        self.fail('Previous operation did not finish')

    def transient_desktop(self):
        live = Mock(wraps=self.desktop)
        live.connect.return_value = live
        calls = []
        def send(text, **kw):
            calls.append(text)
            if len(calls)==1:
                raise ViberRetryable('Typed text arrived too slowly; owned draft cleared.', 'draft_readback')
            return self.desktop.send_message(text,**kw)
        live.send_message.side_effect = send
        self.service.client_factory = lambda:live
        return live

    def test_draft_readback_failure_reuses_generated_text_and_one_send_record(self):
        live = self.transient_desktop()
        self.incoming(); self.replies.tick()
        failed = self.wait_state('RETRY')
        self.assertEqual(self.desktop.sent,[])
        self.assertEqual(failed['error_code'],'draft_readback')
        self.due(); self.replies.tick()
        sent = self.wait_state('DISPATCHED')
        self.assertEqual(sent['id'],failed['id'])
        self.assertEqual(self.desktop.sent,['Zdravo!'])
        self.assertEqual(len(self.generator.contexts),1)
        self.assertEqual(live.open_phone.call_count,2)
        self.replies.tick()
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_reply_jobs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_reply_events').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_sends').fetchone()[0],1)

    def test_generation_timeout_recovers_same_claim(self):
        generate = Mock(side_effect=[ReplyRetryable('Timed out','codex_timeout'),self.generator.result])
        self.generator.generate = generate
        self.incoming(); self.replies.tick()
        old = self.wait_state('RETRY')
        self.due(); self.replies.tick()
        new = self.wait_state('DISPATCHED')
        self.assertEqual(old['id'],new['id'])
        self.assertEqual(generate.call_count,2)
        self.assertEqual(self.desktop.sent,['Zdravo!'])

    def test_backoff_does_not_tie_up_drafting_or_desktop(self):
        self.transient_desktop()
        self.incoming(); self.replies.tick(); self.wait_state('RETRY')
        deadline=time.monotonic()+3
        while self.service.pending and time.monotonic()<deadline:
            time.sleep(.01)
        self.replies.tick()
        self.assertEqual(self.desktop.sent,[])
        self.assertEqual(len(self.generator.contexts),1)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM controller_jobs WHERE state IN ('PENDING','RUNNING')").fetchone()[0],0)

    def test_new_message_supersedes_a_retry_instead_of_sending_old_text(self):
        self.transient_desktop()
        self.incoming(); self.replies.tick(); self.wait_state('RETRY'); self.due()
        self.generator.result = {'action':'reply','text':'Fresh answer','reason':'New message'}
        self.incoming(3); self.replies.tick(); self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent,['Fresh answer'])
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT state FROM auto_reply_jobs WHERE trigger_id=2').fetchone()[0],'STALE')

    def test_manual_reply_ownership_change_and_pause_stop_recovery(self):
        from test_viber_inbox import message
        self.transient_desktop()
        self.incoming(); self.replies.tick(); self.wait_state('RETRY'); self.due()
        self.source.data['messages'].append(message(3,'OUTGOING',body='Manual answer'))
        self.source.data['max_event_id']=3
        self.service.watcher.poll_once(); self.replies.tick(); self.wait_state('STALE')
        self.assertEqual(self.desktop.sent,[])

    def test_explicit_pause_is_preserved_while_a_retry_is_waiting(self):
        self.transient_desktop()
        self.incoming(); self.replies.tick(); self.wait_state('RETRY'); self.due()
        self.replies.configure(False,self.replies.settings()['instructions'])
        self.replies.tick()
        self.assertFalse(self.replies.settings()['enabled'])
        self.assertEqual(self.desktop.sent,[])

    def test_an_attempt_marker_wins_even_if_job_state_is_wrong(self):
        self.transient_desktop()
        self.incoming(); self.replies.tick(); failed=self.wait_state('RETRY'); self.due()
        with closing(connect(self.path)) as db,db:
            db.execute("UPDATE web_sends SET state='UNKNOWN',attempted_at='2026-10-10T17:00:00+00:00' WHERE id=?",(failed['send_id'],))
        self.replies.tick(); self.wait_state('BLOCKED')
        self.assertEqual(self.desktop.sent,[])
        self.assertEqual(len(self.generator.contexts),1)

    def test_restart_resumes_unfinished_generation_with_no_dispatch_attempt(self):
        self.incoming()
        job_id=self.replies._claim()
        self.replies.close()
        self.replies=self.service.replies=AutoReplies(self.service,self.generator,settle_seconds=0)
        self.assertEqual(self.replies._job(job_id)['state'],'RETRY')
        self.due(); self.replies.tick(); self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent,['Zdravo!'])

    def test_temporary_worker_outage_recovers_before_typing(self):
        live=Mock(wraps=self.desktop)
        live.connect.side_effect=[ViberRetryable('Bridge down','worker_unavailable'),live]
        self.service.client_factory=lambda:live
        self.incoming();self.replies.tick();self.wait_state('RETRY')
        self.assertEqual(live.open_phone.call_count,0)
        self.due();self.replies.tick();self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent,['Zdravo!'])

    def test_queue_submission_failure_does_not_lose_the_generated_reply(self):
        submit=self.service.pool.submit
        failures=[]
        def interrupted(*args,**kwargs):
            if not failures:
                failures.append(True)
                raise RuntimeError('Queue temporarily unavailable')
            return submit(*args,**kwargs)
        self.service.pool.submit=interrupted
        self.incoming();self.replies.tick();self.wait_state('RETRY')
        self.assertEqual(self.service.pending,0)
        self.due();self.replies.tick();self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent,['Zdravo!'])
        self.assertEqual(len(self.generator.contexts),1)

    def test_later_reply_does_not_wait_for_an_outgoing_message_removed_from_native_history(self):
        from test_viber_inbox import message
        self.incoming();self.replies.tick();self.wait_state('DISPATCHED')
        self.source.data['messages'].append(message(3,'OUTGOING',body='Zdravo!'))
        self.source.data['max_event_id']=3
        self.service.watcher.poll_once();self.replies.tick()
        with closing(connect(self.path)) as db:
            self.assertIsNotNone(db.execute('SELECT outgoing_confirmed_at FROM auto_reply_jobs').fetchone()[0])
        self.source.data['messages']=[m for m in self.source.data['messages'] if m['event_id']!=3]
        self.generator.result={'action':'reply','text':'Another answer','reason':'Latest question'}
        self.incoming(4);self.replies.tick();self.wait_state('DISPATCHED')
        self.assertEqual(self.desktop.sent,['Zdravo!','Another answer'])

    def test_link_reply_ack_is_confirmed_and_next_question_can_dispatch(self):
        from test_viber_inbox import message
        text='Primer rada: https://example.com/'
        self.generator.result={'action':'reply','text':text,'reason':'Requested example'}
        self.incoming();self.replies.tick();self.wait_state('DISPATCHED')
        pending=message(3,'OUTGOING',body=text,message_type=9,token='0',sort_order=2000,
                        timestamp_ms=int(time.time()*1000)-1000)
        self.source.data['messages'].append(pending)
        self.source.data['max_event_id']=3
        self.service.watcher.poll_once();self.replies.tick()
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT outgoing_event_id FROM auto_reply_jobs').fetchone()[0],3)
        confirmed={**pending,'token':'3000','sort_order':3000,'timestamp_ms':pending['timestamp_ms']+500}
        self.source.data['messages']=[confirmed if m['event_id']==3 else m for m in self.source.data['messages']]
        self.generator.result={'action':'reply','text':'Another answer','reason':'Latest question'}
        self.incoming(4);self.replies.tick();self.wait_state('DISPATCHED')
        self.assertEqual(self.service.watcher.status()['state'],'WATCHING')
        self.assertEqual(self.desktop.sent,[text,'Another answer'])


if __name__=='__main__':
    unittest.main()
