"""Private client context survives edits without creating duplicate replies."""
from contextlib import closing
import threading
import time
import unittest

from app.accounts import stamp
from app.client_descriptions import ClientDescriptions
from app.storage import connect
import test_controller
import test_reply_recovery


class DescriptionTests(unittest.TestCase):
    setUp = test_controller.DraftTests.setUp
    tearDown = test_controller.DraftTests.tearDown
    incoming = test_controller.DraftTests.incoming
    wait_state = test_controller.DraftTests.wait_state

    def save(self, text, revision=0):
        return self.service.client_descriptions.save(1, text, revision)

    def test_persistence_unicode_clear_and_unchanged_recipient_snapshot(self):
        original = self.service.lead(1)
        before = self.replies.settings()
        text = 'Frizerski salon u Nišu.\nŽeli galeriju i zakazivanje.'
        saved = self.save(text)
        self.assertEqual(saved['revision'], 1)
        reopened = ClientDescriptions(self.service)
        with closing(connect(self.path)) as db:
            self.assertEqual(reopened.get(db, 1), {'description': text, 'revision': 1})
        self.assertEqual(self.service.contacts()[0]['client_description'], text)
        self.assertEqual(self.service.lead(1), original)
        self.assertEqual(self.replies.settings(), before)
        self.assertEqual(self.save(text, 1)['revision'], 1)
        self.assertEqual(self.save('', 1)['revision'], 2)
        self.assertEqual(self.service.contacts()[0]['client_description'], '')
        self.replies.tick()
        self.assertEqual(self.replies.jobs(), [])
        self.assertEqual(self.desktop.sent, [])

    def test_stale_tab_validation_and_account_isolation(self):
        self.save('Salon')
        with self.assertRaisesRegex(ValueError, 'drugoj kartici'):
            self.save('Old tab', 0)
        for text, revision in ((None, 1), ('x'*4001, 1), ('bad\0text', 1), ('ok', True)):
            with self.assertRaises(ValueError):
                self.save(text, revision)
        other = self.service.store.create_with_android('+381649876543', 'Other client', lambda *_: None)
        with closing(connect(self.path)) as db, db:
            db.execute("INSERT INTO accounts(id,phone,enabled,created_at) VALUES('other','+381640000000',1,?)", (stamp(),))
            db.execute('INSERT INTO contact_owners(phone,account_id,claimed_at) VALUES(?,?,?)', (other.phone, 'other', stamp()))
            db.execute("INSERT INTO contact_descriptions(account_id,lead_id,description,revision,updated_at) VALUES('other',?,'PRIVATE OTHER ACCOUNT',1,?)", (other.id, stamp()))
        with self.assertRaises(PermissionError):
            self.service.client_descriptions.save(other.id, 'Overwrite', 1)
        self.assertNotIn('PRIVATE OTHER ACCOUNT', str(self.service.contacts()))

    def test_description_enters_only_this_clients_model_context_and_portfolio(self):
        self.save('Frizerski salon; želi galeriju frizura.')
        self.incoming(); self.replies.tick(); self.wait_state('DRAFT')
        context = self.generator.contexts[0]
        self.assertEqual(context['owner_client_description'], 'Frizerski salon; želi galeriju frizura.')
        self.assertNotIn('owner_client_description', context['contact'])
        self.assertEqual(context['portfolio_candidates'][0]['category'], 'Hair salon')
        self.assertEqual(self.desktop.sent, [])

    def test_edit_draft_invalidates_old_approval_and_regenerates_same_turn(self):
        self.incoming(); self.replies.tick(); old = self.wait_state('DRAFT')
        self.save('Želi kontakt formu, ne automatsko zakazivanje.')
        with self.assertRaises(ValueError):
            self.replies.review_draft(old['id'], True)
        self.replies.invalidate_drafts()
        self.replies.tick(); new = self.wait_state('DRAFT')
        self.assertEqual(new['id'], old['id'])
        self.assertEqual(len(self.generator.contexts), 2)
        self.assertIn('kontakt formu', self.generator.contexts[-1]['owner_client_description'])
        self.assertEqual(self.desktop.sent, [])

    def test_edit_during_generation_discards_old_result_then_regenerates(self):
        started, release = threading.Event(), threading.Event()
        self.generator.callback = lambda: (started.set(), release.wait(3))
        self.incoming(); self.replies.tick()
        self.assertTrue(started.wait(2))
        self.save('Klijent već ima logo.')
        release.set(); old = self.wait_state('REGENERATE')
        deadline = time.monotonic()+2
        while self.replies.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.generator.callback = None
        self.replies.tick(); new = self.wait_state('DRAFT')
        self.assertEqual(old['id'], new['id'])
        self.assertEqual(self.generator.contexts[0]['owner_client_description'], '')
        self.assertEqual(self.generator.contexts[-1]['owner_client_description'], 'Klijent već ima logo.')
        self.assertEqual(self.desktop.sent, [])


class AutomaticDescriptionTests(unittest.TestCase):
    setUp = test_reply_recovery.ReplyRecoveryTests.setUp
    tearDown = test_reply_recovery.ReplyRecoveryTests.tearDown
    incoming = test_reply_recovery.ReplyRecoveryTests.incoming
    wait_state = test_reply_recovery.ReplyRecoveryTests.wait_state
    due = test_reply_recovery.ReplyRecoveryTests.due
    transient_desktop = test_reply_recovery.ReplyRecoveryTests.transient_desktop

    def test_edit_before_retry_regenerates_text_and_completed_send_stays_completed(self):
        self.transient_desktop()
        self.incoming(); self.replies.tick(); old = self.wait_state('RETRY')
        self.service.client_descriptions.save(1, 'Ima svoj domen.', 0)
        self.generator.result = {'action': 'reply', 'text': 'Možemo koristiti vaš domen.', 'reason': 'Owner context'}
        self.due(); self.replies.tick(); new = self.wait_state('DISPATCHED')
        self.assertEqual(new['id'], old['id'])
        self.assertEqual(len(self.generator.contexts), 2)
        self.assertEqual(self.desktop.sent, ['Možemo koristiti vaš domen.'])
        self.service.client_descriptions.save(1, 'Ima svoj domen i logo.', 1)
        self.replies.tick()
        self.assertEqual(self.desktop.sent, ['Možemo koristiti vaš domen.'])
        self.assertTrue(self.replies.settings()['enabled'])
        self.assertFalse(self.replies.settings()['review_required'])

    def test_note_changed_after_typing_stops_send_and_rebuilds(self):
        def edit_before_send():
            self.desktop.callback = None
            self.service.client_descriptions.save(1, 'Želi samo kontakt formu.', 0)
        self.desktop.callback = edit_before_send
        self.incoming(); self.replies.tick(); self.wait_state('RETRY')
        self.assertEqual(self.desktop.sent, [])
        self.due(); self.replies.tick(); self.wait_state('DISPATCHED')
        self.assertEqual(len(self.generator.contexts), 2)
        self.assertEqual(self.generator.contexts[-1]['owner_client_description'], 'Želi samo kontakt formu.')
        self.assertEqual(self.desktop.sent, ['Zdravo!'])

    def test_note_edit_never_retries_uncertain_send(self):
        self.desktop.fail = True
        self.incoming(); self.replies.tick(); old = self.wait_state('UNKNOWN')
        self.service.client_descriptions.save(1, 'Ima svoj logo.', 0)
        self.desktop.fail = False
        self.due(); self.replies.tick()
        self.assertEqual(self.replies.jobs()[0]['id'], old['id'])
        self.assertEqual(self.replies.jobs()[0]['state'], 'UNKNOWN')
        self.assertEqual(self.desktop.sent, ['Zdravo!'])


if __name__ == '__main__':
    unittest.main()
