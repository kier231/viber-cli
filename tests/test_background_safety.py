import unittest
from unittest.mock import Mock
from unittest.mock import patch

from ctypes import wintypes

from app.viber import ViberError
from app.viber_background import BackgroundViberClient, _usable_viber_name


class BackgroundSafetyTests(unittest.TestCase):
    def test_background_click_converts_screen_to_client_coordinates(self):
        user32 = Mock()
        user32.ScreenToClient.side_effect = lambda _hwnd, value: (
            setattr(value._obj, "x", 100) or setattr(value._obj, "y", 200) or True)
        user32.GetClientRect.side_effect = lambda _hwnd, value: (
            setattr(value._obj, "left", 0) or setattr(value._obj, "top", 0) or
            setattr(value._obj, "right", 800) or setattr(value._obj, "bottom", 600) or True)
        rect = Mock(left=110, top=210, right=130, bottom=230)
        node = Mock()
        node.rectangle.return_value = rect
        client = BackgroundViberClient(allow_foreground=True)
        client.window = Mock(handle=123)
        client._post = Mock()

        with patch("app.viber_background._USER32", user32), patch("time.sleep"):
            client._click(node)

        packed = (200 << 16) | 100
        self.assertEqual([call.args[2] for call in client._post.call_args_list],
                         [packed, packed, packed])

    def test_stale_info_popup_is_closed_before_dialing(self):
        client = BackgroundViberClient(allow_foreground=True)
        close = object()
        panel = object()
        client.ui = Mock()
        client.ui._type.side_effect = lambda node: "Button" if node is close else "Pane"
        client.ui._auto_id.side_effect = lambda node: (
            "MainWindow.GroupInfoPopup.IconButton_QMLTYPE_56" if node is close else
            "MainWindow.GroupInfoPopup.SideBarContent_QMLTYPE_830")
        states = iter(([close, panel], []))
        client._nodes = Mock(side_effect=lambda: next(states, []))
        client._click = Mock()

        client._dismiss_obscuring_info_popup()

        client._click.assert_called_once_with(close)

    def test_vm_mode_allows_viber_to_own_guest_foreground(self):
        client = BackgroundViberClient(allow_foreground=True)
        client._assert_no_focus_theft()

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
