import unittest

from app.viber import ViberError
from app.viber_background import BackgroundViberClient, _usable_viber_name


class BackgroundSafetyTests(unittest.TestCase):
    def test_unnamed_or_unregistered_chat_is_not_a_recipient(self):
        for name in ("", "Unknown", "My Notes", "No results"):
            with self.subTest(name=name):
                self.assertFalse(_usable_viber_name(name))
        self.assertTrue(_usable_viber_name("Person on Viber"))

    def test_unsupported_emoji_is_rejected_before_draft_entry(self):
        client = BackgroundViberClient()
        with self.assertRaisesRegex(ViberError, "emoji"):
            client.send_message("Hello 🙂")


if __name__ == "__main__":
    unittest.main()
