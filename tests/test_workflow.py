import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.models import LeadStore
from app.viber import ViberError
from main import dispatch


class FakeClient:
    instances = []

    def __init__(self, debug=False):
        self.searched = []
        self.sent = []
        self.header = "Test Company | SJT-1"
        self.verify = True
        self.instances.append(self)

    def connect(self):
        return self

    def search_contact(self, name):
        self.searched.append(name)

    def get_current_contact_name(self):
        return self.header

    def verify_contact(self, lead_id):
        return self.verify

    def send_message(self, text):
        self.sent.append(text)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LeadStore(Path(self.tmp.name) / "leads.db")
        self.lead = self.store.create_with_android("+381641234567", "Test Company",
                                                   lambda name, phone: None)
        FakeClient.instances.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_android_failure_does_not_create_local_lead(self):
        def fail(name, phone):
            raise RuntimeError("Android denied the insert")
        with self.assertRaises(RuntimeError):
            self.store.create_with_android("+381651234567", "Second", fail)
        self.assertEqual(len(self.store.all()), 1)

    def test_send_requires_exact_y(self):
        args = argparse.Namespace(command="send", lead_id=1, message="Hello", debug=False)
        with patch("builtins.input", return_value="yes"):
            dispatch(args, self.store, client_factory=FakeClient)
        self.assertEqual(FakeClient.instances[-1].sent, [])

    def test_header_mismatch_blocks_prompt_and_send(self):
        class WrongClient(FakeClient):
            def __init__(self, debug=False):
                super().__init__(debug)
                self.header = "Another Company | SJT-1"
        args = argparse.Namespace(command="send", lead_id=1, message="Hello", debug=False)
        with patch("builtins.input") as prompt:
            with self.assertRaises(ViberError):
                dispatch(args, self.store, client_factory=WrongClient)
            prompt.assert_not_called()
        self.assertEqual(FakeClient.instances[-1].sent, [])


if __name__ == "__main__":
    unittest.main()
