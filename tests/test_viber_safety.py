import sys
import types
import unittest
from unittest.mock import patch

from app.viber import ViberClient, ViberError


class _Composer:
    def click_input(self):
        pass


class _Client(ViberClient):
    def __init__(self, verifications):
        super().__init__()
        self._expected_name = "Test Company | SJT-1"
        self.verifications = iter(verifications)
        self.searches = []

    def search_contact(self, name):
        self.searches.append(name)

    def verify_contact(self, lead_id):
        return next(self.verifications)

    def _composer(self):
        return _Composer()


class ViberSafetyTests(unittest.TestCase):
    def _send(self, checks):
        client = _Client(checks)
        keys = []
        keyboard = types.ModuleType("pywinauto.keyboard")
        keyboard.send_keys = keys.append
        with patch.dict(sys.modules, {"pywinauto.keyboard": keyboard}), patch("pyperclip.copy"):
            if checks[-1]:
                client.send_message("Hello")
            else:
                with self.assertRaises(ViberError):
                    client.send_message("Hello")
        return client, keys

    def test_header_change_after_paste_prevents_enter(self):
        client, keys = self._send([True, False])
        self.assertEqual(client.searches, ["Test Company | SJT-1"])
        self.assertNotIn("{ENTER}", keys)

    def test_verified_send_uses_clipboard_paste_then_enter(self):
        _, keys = self._send([True, True])
        self.assertEqual(keys, ["^v", "{ENTER}"])


if __name__ == "__main__":
    unittest.main()
