from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest

from app.auto_replies import AutoReplies
from app.codex_replies import DEFAULT_INSTRUCTIONS, validate_result
from app.web_service import WebService
from test_viber_inbox import message, snapshot


class Generator:
    result = {'action': 'reply', 'text': 'Zdravo!', 'reason': 'Greeting'}
    callback = None

    def __init__(self):
        self.contexts = []

    def check_login(self):
        return {'ready': True}

    def generate(self, context, instructions):
        self.contexts.append(context)
        if self.callback:
            self.callback()
        return self.result

    def close(self):
        pass


class Desktop:
    def __init__(self):
        self.sent = []
        self.callback = None
        self.fail = False

    def connect(self):
        return self

    def open_phone(self, phone):
        return 'Person'

    def verify_current_name(self, name):
        return name == 'Person'

    def send_message(self, text, before_dispatch=None, on_dispatch=None, dispatch_lock=None):
        if self.callback:
            self.callback()
        before_dispatch()
        with dispatch_lock:
            on_dispatch()
            self.sent.append(text)
            if self.fail:
                raise ValueError('Uncertain send result')


class Source:
    def __init__(self, data):
        self.data = data

    def read(self, request):
        return self.data

    def close(self):
        pass


class AutoReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ledger.db'
        self.desktop = Desktop()
        self.service = WebService(self.path, lambda: self.desktop, lambda *_: None)
        self.lead = self.service.store.create_with_android('+381641234567', 'Business', lambda *_: None)
        self.generator = Generator()
        self.service.replies.close()
        self.replies = self.service.replies = AutoReplies(self.service, self.generator, settle_seconds=0)
        self.first = message(1, 'OUTGOING', body='Hello', timestamp_ms=int(time.time()*1000)-10000)
        self.source = Source(snapshot(self.first))
        self.service.watcher.source = self.source
        self.service.watcher.poll_once()
        self.replies.configure(True, DEFAULT_INSTRUCTIONS)
        time.sleep(.003)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def incoming(self, *extra):
        self.source.data = snapshot(self.first, *extra)
        self.service.watcher.poll_once()

    def run_reply(self):
        self.replies.tick()
        until = time.monotonic() + 3
        while self.service.pending and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(self.service.pending, 0)

    def test_reply_once_full_context_and_names_preserved(self):
        self.incoming(message(2))
        self.run_reply()
        self.assertEqual(self.desktop.sent, ['Zdravo!'])
        self.assertEqual(self.replies.jobs()[0]['state'], 'DISPATCHED')
        self.run_reply()
        self.assertEqual(self.desktop.sent, ['Zdravo!'])
        lead = self.service.store.get(self.lead.id)
        self.assertEqual((lead.company_name, lead.contact_name, lead.viber_name), ('Business','Business | SJT-1','Person'))
        self.assertEqual(len(self.generator.contexts[0]['messages']), 2)

    def test_enabling_and_reenabling_skip_backlog(self):
        self.replies.configure(False, DEFAULT_INSTRUCTIONS)
        self.incoming(message(2))
        self.replies.configure(True, DEFAULT_INSTRUCTIONS)
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])

    def test_outgoing_never_generates_reply(self):
        self.incoming(message(2,'OUTGOING'))
        self.run_reply()
        self.assertEqual(self.generator.contexts, [])

    def test_burst_coalesced_into_one_reply(self):
        self.incoming(message(2), message(3), message(4))
        self.run_reply()
        self.assertEqual(len(self.desktop.sent), 1)
        self.assertEqual(len(self.replies.jobs()), 1)
        with closing(self.replies.inbox.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auto_reply_events').fetchone()[0], 3)

    def test_manual_reply_supersedes_incoming(self):
        self.incoming(message(2),message(3,'OUTGOING'))
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])
        self.assertEqual(self.replies.jobs()[0]['state'], 'HELD')

    def test_new_message_during_generation_cancels_draft(self):
        second = message(2)
        self.incoming(second)
        self.generator.callback = lambda: self.incoming(second,message(3))
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])
        self.assertEqual(self.replies.jobs()[0]['state'], 'HELD')
        self.generator.callback = None
        self.run_reply()
        self.assertEqual(len(self.desktop.sent), 1)

    def test_change_after_typing_blocks_before_send(self):
        second = message(2)
        self.incoming(second)
        self.desktop.callback = lambda: self.incoming(second,message(3,'OUTGOING'))
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])
        self.assertEqual(self.replies.jobs()[0]['state'], 'BLOCKED')

    def test_pause_during_generation_cancels_reply(self):
        self.incoming(message(2))
        self.generator.callback = lambda: self.replies.configure(False, DEFAULT_INSTRUCTIONS)
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])

    def test_per_conversation_pause_skips_and_resume_does_not_replay(self):
        self.replies.conversation_control('source-a',10,False)
        self.incoming(message(2))
        self.run_reply()
        self.replies.conversation_control('source-a',10,True)
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])

    def test_hold_or_invalid_output_never_sends_and_never_retries(self):
        self.generator.result = {'action':'hold','text':'','reason':'Need the owner to supply a price.'}
        self.incoming(message(2))
        self.run_reply()
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])
        self.assertEqual(len(self.generator.contexts), 1)
        self.assertEqual(self.replies.jobs()[0]['state'], 'HELD')
        for result in ({'action':'reply','text':'','reason':''}, {'action':'reply','text':'Hi\nthere','reason':''},
                       {'action':'hold','text':'send this','reason':''}, {'action':'reply','text':'Hi','reason':'','other':1}):
            with self.assertRaises(ValueError):
                validate_result(result)

    def test_uncertain_dispatch_pauses_chat_and_never_retries(self):
        self.desktop.fail = True
        self.incoming(message(2))
        self.run_reply()
        self.assertEqual(self.replies.jobs()[0]['state'], 'UNKNOWN')
        self.assertEqual(self.replies.inbox.conversations()[0]['reply_enabled'], 0)
        self.run_reply()
        self.assertEqual(len(self.desktop.sent), 1)

    def test_full_history_is_not_limited_to_ui_page(self):
        history = [self.first] + [message(i,'OUTGOING', timestamp_ms=self.first['timestamp_ms']+i) for i in range(2,152)]
        self.source.data = snapshot(*history)
        self.service.watcher.poll_once()
        self.source.data = snapshot(*history,message(152))
        self.service.watcher.poll_once()
        self.run_reply()
        self.assertEqual(len(self.generator.contexts[0]['messages']), 152)

    def test_contact_change_during_generation_is_held(self):
        self.incoming(message(2))
        self.generator.callback = lambda: self.service.store.set_viber_name(self.lead.id,'Different person')
        self.run_reply()
        self.assertEqual(self.desktop.sent, [])

    def test_restart_does_not_replay_claimed_turn(self):
        self.incoming(message(2))
        job_id = self.replies._claim()
        self.assertIsNotNone(job_id)
        restarted = AutoReplies(self.service,self.generator,settle_seconds=0)
        self.assertEqual(restarted.jobs()[0]['state'],'INTERRUPTED')
        restarted.tick()
        self.assertEqual(self.desktop.sent, [])

    def test_media_or_ambiguous_sender_does_not_generate(self):
        self.incoming(message(2,message_type=2),message(3,sender_verified=False))
        self.run_reply()
        self.assertEqual(self.generator.contexts, [])

    def test_next_reply_waits_for_native_outgoing_confirmation(self):
        second = message(2)
        self.incoming(second)
        self.run_reply()
        fourth = message(4,timestamp_ms=second['timestamp_ms']+2)
        self.incoming(second,fourth)
        self.run_reply()
        self.assertEqual(len(self.desktop.sent),1)
        self.assertEqual(self.replies.state,'WAITING_FOR_OUTGOING')
        self.incoming(second,message(3,'OUTGOING',body='Zdravo!',timestamp_ms=second['timestamp_ms']+1),
                      fourth)
        self.run_reply()
        self.assertEqual(len(self.desktop.sent),2)

    def test_reviewed_unknown_does_not_block_future_turn_or_repause_after_restart(self):
        second = message(2)
        self.desktop.fail = True
        self.incoming(second)
        self.run_reply()
        self.replies.conversation_control('source-a',10,True)
        restarted = AutoReplies(self.service,self.generator,settle_seconds=0)
        self.assertEqual(restarted.inbox.conversations()[0]['reply_enabled'],1)
        self.desktop.fail = False
        self.incoming(second,message(3))
        self.run_reply()
        self.assertEqual(len(self.desktop.sent),2)


if __name__ == '__main__':
    unittest.main()
