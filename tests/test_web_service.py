from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from app.models import Message
from app.viber import ViberError
from app.web_service import WebService, validate_message


class FakeDesktop:
    name = "Viber Person"
    fail_send = False
    sent = []

    def connect(self):
        return self

    def open_phone(self, phone):
        return self.name

    def verify_current_name(self, name):
        return self.name == name

    def send_message(self, text):
        self.sent.append(text)
        if self.fail_send:
            raise ViberError("The result is uncertain.")

    def read_messages(self):
        return [Message("Visible message")]


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "leads.sqlite3"
        FakeDesktop.name = "Viber Person"
        FakeDesktop.fail_send = False
        FakeDesktop.sent.clear()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.lead = self.service.store.create_with_android("+381641234567", "Business", lambda *_: None)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def completed(self, task):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.service.operation(task["operation_id"])
            if result["state"] not in {"QUEUED", "RUNNING"}:
                return result
            time.sleep(.01)
        self.fail("Operation did not finish")

    def preview(self):
        result = self.completed(self.service.prepare(self.lead.id, "Zdravo čćšžđ"))
        self.assertEqual(result["state"], "SUCCEEDED", result["error"])
        return result["result"]

    def payload(self, preview):
        return {"preview_token": preview["token"], "request_key": str(uuid.uuid4()), "confirmed": True}

    def test_preview_discovers_name_and_never_sends(self):
        preview = self.preview()
        lead = self.service.store.get(self.lead.id)
        self.assertEqual(preview["viber_name"], "Viber Person")
        self.assertEqual(lead.company_name, "Business")
        self.assertEqual(lead.contact_name, "Business | SJT-1")
        self.assertEqual(lead.viber_name, "Viber Person")
        self.assertEqual(FakeDesktop.sent, [])

    def test_add_contact_verifies_viber_without_android_or_sending(self):
        self.service.android_add = Mock(side_effect=AssertionError("Android must not be called"))
        result = self.completed(self.service.add_contact({"phone": "065 123 4567", "company_name": "  New   Salon  "}))
        self.assertEqual(result['state'], 'SUCCEEDED', result['error'])
        lead = self.service.store.get(result['result']['id'])
        self.assertEqual((lead.phone, lead.company_name, lead.viber_name),
                         ('+381651234567', 'New Salon', 'Viber Person'))
        self.assertEqual(lead.contact_name, f'New Salon | SJT-{lead.id}')
        self.service.android_add.assert_not_called()
        self.assertEqual(FakeDesktop.sent, [])
        self.assertEqual(self.service.records('sent'), [])

    def test_unusable_or_changed_recipient_is_not_saved(self):
        for name in ('Unknown', 'My Notes', 'not on Viber'):
            FakeDesktop.name = name
            result = self.completed(self.service.add_contact({'phone': '0651234567', 'company_name': 'Salon'}))
            self.assertEqual(result['state'], 'FAILED')
        FakeDesktop.name = 'Viber Person'
        with patch.object(FakeDesktop, 'verify_current_name', side_effect=[True, False]):
            result = self.completed(self.service.add_contact({'phone': '0651234567', 'company_name': 'Salon'}))
        self.assertEqual(result['state'], 'FAILED')
        self.assertEqual(len(self.service.contacts()), 1)
        self.assertEqual(FakeDesktop.sent, [])

    def test_invalid_contact_input_never_opens_viber(self):
        with patch.object(FakeDesktop, 'open_phone') as opened:
            for company in (None, '', ' ', 'bad|name', 'bad\nname', 'x' * 121):
                with self.assertRaises(ValueError):
                    self.service.add_contact({'phone': '0651234567', 'company_name': company})
            with self.assertRaises(ValueError):
                self.service.add_contact({'phone': '123', 'company_name': 'Salon'})
            with self.assertRaises(ValueError):
                self.service.add_contact({'phone': self.lead.phone, 'company_name': 'Duplicate'})
            opened.assert_not_called()

    def test_queued_duplicate_additions_save_one_contact(self):
        payload = {'phone': '0651234567', 'company_name': 'Salon'}
        with patch.object(self.service, '_check_new_contact'):
            tasks = [self.service.add_contact(payload), self.service.add_contact(payload)]
        results = [self.completed(task) for task in tasks]
        self.assertEqual(sorted(result['state'] for result in results), ['FAILED', 'SUCCEEDED'])
        self.assertEqual(len([lead for lead in self.service.contacts() if lead['phone'] == '+381651234567']), 1)
        self.assertEqual(FakeDesktop.sent, [])

    def test_confirmation_required_and_retry_never_sends_twice(self):
        preview = self.preview()
        payload = self.payload(preview)
        with self.assertRaises(ValueError):
            self.service.send({**payload, "confirmed": False})
        first = self.service.send(payload)
        self.assertEqual(self.completed(first)["state"], "SUCCEEDED")
        second = self.service.send(payload)
        self.assertEqual(first, second)
        self.assertEqual(FakeDesktop.sent, [preview["text"]])
        # Idempotency survives a new server process and a consumed preview.
        self.service.close()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.assertEqual(first, self.service.send(payload))
        self.assertEqual(len(FakeDesktop.sent), 1)

    def test_expired_preview_and_changed_contact_block_send(self):
        preview = self.preview()
        self.service.previews[preview["token"]]["expires_at"] = 0
        with self.assertRaisesRegex(ValueError, "expired"):
            self.service.send(self.payload(preview))
        preview = self.preview()
        self.service.store.set_viber_name(self.lead.id, "Changed name")
        with self.assertRaisesRegex(ValueError, "changed after review"):
            self.service.send(self.payload(preview))
        self.assertEqual(self.service.records("sent"), [])
        self.assertEqual(FakeDesktop.sent, [])

    def test_saved_name_mismatch_does_not_block_verified_number(self):
        self.service.store.set_viber_name(self.lead.id, 'Mihajlo')
        FakeDesktop.name = 'Brt'
        with patch.object(FakeDesktop,'open_phone',autospec=True,side_effect=lambda _client, phone: FakeDesktop.name) as opened:
            preview = self.preview()
        self.assertEqual(opened.call_args.args[1],self.lead.phone)
        self.assertEqual(preview['viber_name'],'Brt')
        self.assertEqual(preview['lead']['viber_name'],'Mihajlo')
        self.assertEqual(FakeDesktop.sent,[])

    def test_changed_display_name_after_review_sends_to_same_phone(self):
        preview = self.preview()
        FakeDesktop.name = "New display name"
        result = self.completed(self.service.send(self.payload(preview)))
        self.assertEqual(result['state'],'SUCCEEDED',result['error'])
        self.assertEqual(self.service.records('sent')[0]['viber_name'],'New display name')
        self.assertEqual(FakeDesktop.sent,[preview['text']])

    def test_unstable_open_conversation_still_blocks_send(self):
        preview = self.preview()
        with patch.object(FakeDesktop,'verify_current_name',return_value=False):
            result = self.completed(self.service.send(self.payload(preview)))
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(self.service.records("sent")[0]["state"], "BLOCKED")
        self.assertEqual(FakeDesktop.sent, [])

    def test_saved_phone_change_after_review_still_blocks_send(self):
        preview = self.preview()
        with closing(self.service.store._connect()) as db,db:
            db.execute('UPDATE leads SET phone=? WHERE id=?',('+381641111112',self.lead.id))
        with self.assertRaisesRegex(ValueError,'changed after review'):
            self.service.send(self.payload(preview))
        self.assertEqual(FakeDesktop.sent,[])

    def test_uncertain_send_is_recorded_and_never_retried(self):
        preview = self.preview()
        FakeDesktop.fail_send = True
        payload = self.payload(preview)
        task = self.service.send(payload)
        self.assertEqual(self.completed(task)["state"], "FAILED")
        self.assertEqual(self.service.records("sent")[0]["state"], "UNKNOWN")
        self.service.send(payload)
        self.assertEqual(len(FakeDesktop.sent), 1)

    def test_invalid_text_is_rejected_before_opening_viber(self):
        for text in ("", "hello\nthere", "emoji 😀", "\ud800", "x" * 4001):
            with self.assertRaises(ValueError):
                validate_message(text)
        self.assertEqual(validate_message("čćšžđ"), "čćšžđ")

    def test_visible_read_is_stored_without_guessing_direction(self):
        result = self.completed(self.service.open_contact(self.lead.id, read=True))
        self.assertEqual(result["state"], "SUCCEEDED")
        records = self.service.records("inbox")
        self.assertEqual(records[0]["messages"], [{"text": "Visible message", "direction": "MESSAGE"}])

    def test_restart_marks_interrupted_attempt_unknown(self):
        with closing(self.service.store._connect()) as db, db:
            db.execute("INSERT INTO web_operations(id,kind,state,created_at,result,error) VALUES('interrupted','send','RUNNING',?,NULL,NULL)", ("2026-01-01",))
            db.execute("INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,phone,company_name,"
                       "viber_name,text,state,created_at,updated_at,error) VALUES('s','k','h','interrupted',1,'+381641234567',"
                       "'Business','Person','Text','SUBMITTING','2026-01-01','2026-01-01',NULL)")
        self.service.close()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.assertEqual(self.service.operation("interrupted")["state"], "INTERRUPTED")
        self.assertEqual(self.service.records("sent")[0]["state"], "UNKNOWN")
        self.assertEqual(FakeDesktop.sent, [])


if __name__ == "__main__":
    unittest.main()
