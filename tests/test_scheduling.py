from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from app.scheduling import parse_schedule
from app.web_service import WebService
from test_web_service import FakeDesktop


class ScheduleTimeTests(unittest.TestCase):
    reference = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_warsaw_offset_changes_with_daylight_saving(self):
        for local_time, expected in (("2026-02-01T12:30", "2026-02-01T11:30:00+00:00"),
                                     ("2026-07-01T12:30", "2026-07-01T10:30:00+00:00")):
            result = parse_schedule({"mode": "datetime", "local_time": local_time}, self.reference)
            self.assertEqual(result["scheduled_at"], expected)
            self.assertEqual(result["time_zone"], "Europe/Warsaw")

    def test_repeated_or_nonexistent_time_requires_another_choice(self):
        for local_time in ("2026-03-29T02:30", "2026-10-25T02:30"):
            with self.assertRaisesRegex(ValueError, "daylight saving"):
                parse_schedule({"mode": "datetime", "local_time": local_time}, self.reference)

    def test_past_and_invalid_delays_are_rejected(self):
        for schedule in ({"mode": "delay", "minutes": 0}, {"mode": "delay", "minutes": True},
                         {"mode": "delay", "minutes": 1.5}, {"mode": "delay", "minutes": 525601},
                         {"mode": "datetime", "local_time": "2025-12-31T12:00"},
                         {"mode": "datetime", "local_time": "2027-12-31T12:00"}):
            with self.assertRaises(ValueError):
                parse_schedule(schedule, self.reference)
        self.assertEqual(parse_schedule({"mode": "delay", "minutes": 60}, self.reference)["scheduled_at"],
                         "2026-01-01T01:00:00+00:00")


class ScheduledSendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
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

    def schedule(self):
        result = self.completed(self.service.prepare(self.lead.id, "Later message", {"mode": "delay", "minutes": 60}))
        self.assertEqual(result["state"], "SUCCEEDED", result["error"])
        preview = result["result"]
        payload = {"preview_token": preview["token"], "request_key": str(uuid.uuid4()), "confirmed": True}
        task = self.service.send(payload)
        self.assertEqual(self.completed(task)["result"]["state"], "SCHEDULED")
        self.assertEqual(FakeDesktop.sent, [])
        return task, payload

    def make_due(self, send_id, minutes_late=0):
        due = (datetime.now(timezone.utc) - timedelta(minutes=minutes_late, seconds=1)).isoformat(timespec="seconds")
        with closing(self.service.store._connect()) as db, db:
            db.execute("UPDATE web_sends SET scheduled_at=? WHERE id=?", (due, send_id))

    def test_confirmed_schedule_is_saved_and_cannot_be_duplicated(self):
        task, payload = self.schedule()
        self.assertEqual(self.service.send(payload), task)
        self.assertEqual(len(self.service.records("scheduled")), 1)
        self.assertEqual(self.service.records("sent"), [])
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(FakeDesktop.sent, [])

    def test_cancelled_schedule_never_dispatches(self):
        task, _ = self.schedule()
        self.service.cancel_scheduled(task["send_id"])
        self.make_due(task["send_id"])
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(self.service.records("scheduled")[0]["state"], "CANCELLED")
        self.assertEqual(FakeDesktop.sent, [])

    def test_restart_preserves_schedule_then_dispatches_exactly_once(self):
        task, payload = self.schedule()
        self.service.close()
        self.service = WebService(self.path, FakeDesktop, lambda *_: None)
        self.assertEqual(self.service.send(payload), task)
        self.assertEqual(self.service.records("scheduled")[0]["state"], "SCHEDULED")
        self.make_due(task["send_id"])
        dispatch = self.service._dispatch_due()
        self.assertEqual(self.completed(dispatch)["state"], "SUCCEEDED")
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(FakeDesktop.sent, ["Later message"])
        self.assertEqual(self.service.records("sent")[0]["state"], "DISPATCHED")
        with self.assertRaises(ValueError):
            self.service.cancel_scheduled(task["send_id"])

    def test_overdue_schedule_is_missed_without_sending(self):
        task, _ = self.schedule()
        self.make_due(task["send_id"], minutes_late=16)
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(self.service.records("scheduled")[0]["state"], "MISSED")
        self.assertEqual(FakeDesktop.sent, [])

    def test_renamed_recipient_can_receive_scheduled_message_at_same_phone(self):
        task, _ = self.schedule()
        self.make_due(task["send_id"])
        FakeDesktop.name = "New display name"
        dispatch = self.service._dispatch_due()
        self.assertEqual(self.completed(dispatch)["state"], "SUCCEEDED")
        self.assertEqual(self.service.records("scheduled")[0]["state"], "DISPATCHED")
        self.assertEqual(FakeDesktop.sent, ['Later message'])

    def test_busy_desktop_worker_defers_due_schedule(self):
        task, _ = self.schedule()
        self.make_due(task["send_id"])
        release = threading.Event()
        occupied = self.service._enqueue("occupied", lambda: release.wait(5))
        try:
            self.assertIsNone(self.service._dispatch_due())
            self.assertEqual(self.service.records("scheduled")[0]["state"], "SCHEDULED")
            self.assertEqual(FakeDesktop.sent, [])
        finally:
            release.set()
        self.completed(occupied)
        # The operation becomes terminal just before the worker releases its busy count.
        deadline = time.monotonic() + 5
        while self.service.pending and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.completed(self.service._dispatch_due())["state"], "SUCCEEDED")
        self.assertEqual(FakeDesktop.sent, ["Later message"])

    def test_scheduler_thread_runs_due_message_without_browser_polling(self):
        task, _ = self.schedule()
        self.make_due(task["send_id"])
        self.service.start_scheduler()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.service.records("scheduled")[0]["state"] == "DISPATCHED":
                break
            time.sleep(.02)
        self.assertEqual(FakeDesktop.sent, ["Later message"])
        self.assertEqual(self.service.records("scheduled")[0]["state"], "DISPATCHED")

    def test_uncertain_scheduled_send_is_never_retried(self):
        task, _ = self.schedule()
        self.make_due(task["send_id"])
        FakeDesktop.fail_send = True
        dispatch = self.service._dispatch_due()
        self.assertEqual(self.completed(dispatch)["state"], "FAILED")
        self.assertEqual(self.service.records("scheduled")[0]["state"], "UNKNOWN")
        self.assertIsNone(self.service._dispatch_due())
        self.assertEqual(len(FakeDesktop.sent), 1)


if __name__ == "__main__":
    unittest.main()
