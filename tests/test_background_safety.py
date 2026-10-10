import unittest
from unittest.mock import Mock
from unittest.mock import patch

from ctypes import wintypes

from app.viber import ViberError
from app.viber_background import BackgroundViberClient, _usable_viber_name
from app.reply_errors import ViberRetryable


class BackgroundSafetyTests(unittest.TestCase):
    def menu_client(self, states):
        client = BackgroundViberClient(allow_foreground=True)
        client.ui = Mock()
        client.ui._type.side_effect = lambda n: n['type']
        client.ui._auto_id.side_effect = lambda n: n.get('id', '')
        client.ui._name.side_effect = lambda n: n.get('name', '')
        snapshots = iter(states)
        client._nodes = Mock(side_effect=lambda: next(snapshots, states[-1]))
        client._click = Mock()
        return client

    def test_existing_profile_menu_opens_dial_without_profile_button(self):
        dial = {'type': 'Button', 'name': 'Use dial pad'}
        pad = Mock()
        pad.__getitem__ = Mock(side_effect=lambda k: {'type': 'Pane', 'id': 'MainWindow.ProfilePopup'}[k])
        pad.get = lambda k, default='': {'id': 'MainWindow.ProfilePopup'}.get(k, default)
        pad.children.return_value = [{'type': 'Edit'}]
        client = self.menu_client([[dial], [pad]])
        with patch('time.sleep'):
            self.assertIs(client._open_dial_pad(), pad)
        client._click.assert_called_once_with(dial)

    def test_existing_dial_pad_is_reused_without_toggling_menu(self):
        pad = Mock()
        pad.get = lambda k, default='': {'id': 'MainWindow.ProfilePopup'}.get(k, default)
        pad.children.return_value = [{'type': 'Edit'}]
        client = self.menu_client([[pad]])
        self.assertIs(client._open_dial_pad(), pad)
        client._click.assert_not_called()

    def test_number_readback_mismatch_stops_before_opening_a_chat(self):
        client = BackgroundViberClient(allow_foreground=True)
        client.ui = Mock()
        client.ui._type.side_effect = lambda node: node.kind
        client.ui._auto_id.side_effect = lambda node: node.aid
        field = Mock(kind='Edit',aid='PhoneField')
        field.get_value.side_effect = ['', '', '+', '+4']
        keys = [Mock(kind='Button',aid='RoundIconButton_' + str(i)) for i in range(12)]
        for i,key in enumerate(keys):
            key.rectangle.return_value = Mock(top=i//3,left=i%3)
        message_button = Mock(kind='Button',aid='SmallIconButton_message')
        pad = Mock()
        pad.children.return_value = [field,*keys,message_button]
        client._dismiss_obscuring_info_popup = Mock()
        client._open_dial_pad = Mock(return_value=pad)
        client._click = Mock()
        with self.assertRaisesRegex(ViberError,'number differed'):
            client._dial('+381641234567')
        self.assertNotIn(message_button,[call.args[0] for call in client._click.call_args_list])

    def test_profile_button_role_and_delayed_popup_are_supported(self):
        profile = {'type': 'Button', 'id': 'MainWindow.ProfileButton_QMLTYPE_123'}
        dial = {'type': 'Button', 'name': 'Use dial pad'}
        pad = Mock()
        pad.get = lambda k, default='': {'id': 'MainWindow.ProfilePopup'}.get(k, default)
        pad.children.return_value = [{'type': 'Edit'}]
        client = self.menu_client([[profile], [profile], [dial], [dial], [pad]])
        with patch('time.sleep'):
            self.assertIs(client._open_dial_pad(), pad)
        self.assertEqual([c.args[0] for c in client._click.call_args_list], [profile, dial])

    def test_ambiguous_profile_or_dial_buttons_stop_without_clicking(self):
        for node in ({'type': 'Button', 'id': 'MainWindow.ProfileButton_QMLTYPE_123'},
                     {'type': 'Button', 'name': 'Use dial pad'}):
            client = self.menu_client([[node, node]])
            with self.assertRaisesRegex(ViberError, 'Multiple'):
                client._open_dial_pad()
            client._click.assert_not_called()

    def test_missing_controls_wait_then_fail_without_clicking(self):
        client = self.menu_client([[]])
        with patch('time.monotonic', side_effect=[0, 0, 1, 2, 3]), patch('time.sleep'):
            with self.assertRaisesRegex(ViberRetryable, 'dial pad is not ready'):
                client._open_dial_pad()
        client._click.assert_not_called()

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

    def test_minimized_viber_is_restored_only_inside_the_vm(self):
        user32=Mock()
        user32.IsIconic.side_effect=[True,False]
        client=BackgroundViberClient(allow_foreground=True)
        client.ui.connect=Mock()
        client.ui.window=Mock(handle=123)
        with patch('app.viber_background._USER32',user32):
            self.assertIs(client.connect(),client)
        user32.ShowWindow.assert_called_once_with(123,9)

    def test_background_host_mode_does_not_restore_or_take_focus(self):
        user32=Mock()
        user32.IsIconic.return_value=True
        client=BackgroundViberClient(allow_foreground=False)
        client.ui.connect=Mock()
        client.ui.window=Mock(handle=123)
        with patch('app.viber_background._USER32',user32):
            with self.assertRaisesRegex(ViberError,'Restore Viber'):
                client.connect()
        user32.ShowWindow.assert_not_called()

    def test_unnamed_or_unregistered_chat_is_not_a_recipient(self):
        for name in ("", "Unknown", "My Notes", "No results"):
            with self.subTest(name=name):
                self.assertFalse(_usable_viber_name(name))
        self.assertTrue(_usable_viber_name("Person on Viber"))

    def test_unsupported_emoji_is_rejected_before_draft_entry(self):
        client = BackgroundViberClient()
        with self.assertRaisesRegex(ViberError, "emoji"):
            client.send_message("Hello 🙂")

    def draft_client(self, values):
        client=BackgroundViberClient(allow_foreground=True)
        client.expected_name='Person'
        client.verify_current_name=Mock(return_value=True)
        composer=Mock()
        composer.get_value.side_effect=values
        client.ui._composer=Mock(return_value=composer)
        client._post=Mock()
        client._click=Mock()
        client._one=Mock(return_value=Mock(is_enabled=lambda:True))
        return client,composer

    def test_delayed_unicode_readback_finishes_before_preparing(self):
        client,composer=self.draft_client(['','', 'Raz','Razumem, hvala!'])
        with patch('time.sleep'):
            client.prepare_message('Razumem, hvala!')
        self.assertIsNotNone(client.prepared)
        client._click.assert_called_once_with(composer)

    def test_slow_prefix_is_cleared_and_reported_as_retryable_without_send(self):
        client,composer=self.draft_client(['','Ra','Ra',''])
        client._erase_draft=Mock()
        with patch('time.monotonic',side_effect=[0,3,0]),patch('time.sleep'):
            with self.assertRaises(ViberRetryable) as raised:
                client.prepare_message('Razumem')
        self.assertEqual(raised.exception.code,'draft_readback')
        self.assertIsNone(client.prepared)
        client._erase_draft.assert_called_once()
        self.assertEqual(client._click.call_count,1)

    def test_foreign_draft_is_not_erased_or_retried(self):
        client,composer=self.draft_client(['','Foreign text','Foreign text'])
        client._erase_draft=Mock()
        with self.assertRaisesRegex(ViberError,'unexpected text'):
            client.prepare_message('Razumem')
        client._erase_draft.assert_not_called()

    def test_changed_recipient_is_not_erased_or_sent(self):
        client,composer=self.draft_client(['','Razumem'])
        client.verify_current_name.side_effect=[True,False,False]
        client._erase_draft=Mock()
        with self.assertRaisesRegex(ViberError,'Recipient changed'):
            client.prepare_message('Razumem')
        client._erase_draft.assert_not_called()
        self.assertIsNone(client.prepared)


if __name__ == "__main__":
    unittest.main()
