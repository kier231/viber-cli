from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
import uuid

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
        result = self.completed(self.service.send(self.payload(preview)))
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(self.service.records("sent")[0]["state"], "BLOCKED")
        self.assertEqual(FakeDesktop.sent, [])

    def test_changed_viber_header_after_review_blocks_send(self):
        preview = self.preview()
        FakeDesktop.name = "Different person"
        result = self.completed(self.service.send(self.payload(preview)))
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(self.service.records("sent")[0]["state"], "BLOCKED")
        self.assertEqual(FakeDesktop.sent, [])

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
            db.execute("INSERT INTO web_operations VALUES('interrupted','send','RUNNING',?,NULL,NULL)", ("2026-01-01",))
            db.execute("INSERT INTO web_sends VALUES('s','k','h','interrupted',1,'+381641234567',"
                       "'Business','Person','Text','SUBMITTING','2026-01-01','2026-01-01',NULL)")
        self.service.close()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.assertEqual(self.service.operation("interrupted")["state"], "INTERRUPTED")
        self.assertEqual(self.service.records("sent")[0]["state"], "UNKNOWN")
        self.assertEqual(FakeDesktop.sent, [])


if __name__ == "__main__":
    unittest.main()
