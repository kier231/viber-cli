from concurrent.futures import ThreadPoolExecutor
import sys
import unittest
from unittest.mock import Mock, patch

from app.vm_agent import Agent
from app.viber import ViberError


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.desktop_pool = ThreadPoolExecutor(max_workers=1)
        self.agent.desktop = Mock()
        self.agent.draft = None
        self.agent.draft_request_id = None
        self.agent.draft_text = None
        self.agent.prepare_outcomes = {}
        self.agent.desktop.prepared = None
        self.agent.desktop.ui._composer().get_value.return_value = ''
        self.agent.desktop.reset_mock()
        self.com = patch.dict(sys.modules, {'pythoncom': Mock(COINIT_MULTITHREADED=0)})
        self.com.start()

    def tearDown(self):
        self.agent.desktop_pool.shutdown(wait=True)
        self.com.stop()

    def test_open_reacquires_window_before_recipient_navigation(self):
        self.agent.desktop.open_phone.return_value = 'Verified recipient'
        self.assertEqual(self.agent.ui('open', {'phone': '+381641234567'}), {'name': 'Verified recipient'})
        self.assertEqual([call[0] for call in self.agent.desktop.mock_calls], ['connect', 'open_phone'])

    def test_pending_draft_cannot_be_navigated_away_from(self):
        self.agent.draft = 'pending'
        with self.assertRaisesRegex(ValueError, 'draft is pending'):
            self.agent.ui('open', {'phone': '+381641234567'})
        self.agent.desktop.connect.assert_not_called()
        self.agent.desktop.open_phone.assert_not_called()

    def test_repeated_prepare_key_returns_same_token_without_retyping(self):
        first=self.agent.ui('prepare',{'request_id':'one','text':'Hello'})
        second=self.agent.ui('prepare',{'request_id':'one','text':'Hello'})
        self.assertEqual(first,second)
        self.agent.desktop.prepare_message.assert_called_once_with('Hello')
        with self.assertRaisesRegex(ViberError,'different text'):
            self.agent.ui('prepare',{'request_id':'one','text':'Changed'})

    def test_cleanup_is_scoped_to_the_request_that_typed_the_draft(self):
        self.agent.ui('prepare',{'request_id':'one','text':'Hello'})
        with self.assertRaisesRegex(ViberError,'different request'):
            self.agent.ui('cancel-request',{'request_id':'two'})
        self.agent.desktop.cancel_prepared.assert_not_called()
        self.assertEqual(self.agent.ui('cancel-request',{'request_id':'one'}),{'clean':True})
        self.assertIsNone(self.agent.draft)

    def test_dispatched_request_cannot_be_prepared_or_cleared_for_retry(self):
        prepared=self.agent.ui('prepare',{'request_id':'one','text':'Hello'})
        self.agent.ui('dispatch',prepared)
        for action,payload in [('prepare',{'request_id':'one','text':'Hello'}),('cancel-request',{'request_id':'one'})]:
            with self.assertRaisesRegex(ViberError,'already dispatched'):
                self.agent.ui(action,payload)
        self.agent.desktop.prepare_message.assert_called_once()

    def test_leftover_unowned_text_is_not_cleared(self):
        self.agent.desktop.ui._composer().get_value.return_value='Human draft'
        with self.assertRaisesRegex(ViberError,'unverified draft'):
            self.agent.ui('cancel-request',{'request_id':'unknown'})
        self.agent.desktop.cancel_prepared.assert_not_called()

    def test_cancel_does_not_drop_its_token_until_empty_draft_is_confirmed(self):
        prepared=self.agent.ui('prepare',{'request_id':'one','text':'Hello'})
        self.agent.desktop.ui._composer().get_value.return_value='Hello'
        with self.assertRaisesRegex(ViberError,'cleanup was not confirmed'):
            self.agent.ui('cancel',prepared)
        self.assertEqual(self.agent.draft,prepared['draft'])

    def test_failed_window_reacquisition_does_not_open_or_send(self):
        self.agent.desktop.connect.side_effect = ViberError('Viber window unavailable')
        with self.assertRaisesRegex(ViberError, 'unavailable'):
            self.agent.ui('open', {'phone': '+381641234567'})
        self.agent.desktop.open_phone.assert_not_called()
        self.agent.desktop.dispatch_prepared.assert_not_called()

    def test_authenticated_inspection_does_not_change_the_desktop(self):
        desktop = self.agent.desktop
        desktop._nodes.return_value = [object()]
        desktop.ui._type.return_value = 'Button'
        desktop.ui._name.return_value = 'Use dial pad'
        desktop.ui._auto_id.return_value = 'MainWindow.ProfilePopup.DialButton'
        result = self.agent.ui('inspect', {})
        self.assertEqual(result['controls'][0]['name'], 'Use dial pad')
        self.assertFalse(result['draft_pending'])
        desktop.connect.assert_not_called()
        desktop.open_phone.assert_not_called()
        desktop.dispatch_prepared.assert_not_called()

    def test_unlinked_account_cannot_open_prepare_or_dispatch(self):
        self.agent.activation_state = 'AWAITING_NUMBER'
        for action in ('open', 'prepare', 'dispatch'):
            with self.assertRaisesRegex(ValueError, 'Link the new Viber account'):
                self.agent.ui(action, {})
        self.agent.desktop.connect.assert_not_called()
        self.agent.desktop.prepare_message.assert_not_called()
        self.agent.desktop.dispatch_prepared.assert_not_called()

    def test_unlinked_account_cannot_supply_history(self):
        self.agent.activation_state = 'AWAITING_NUMBER'
        with self.assertRaisesRegex(ValueError, 'not been linked'):
            self.agent.read({})

    def test_health_distinguishes_pairing_from_usable_account(self):
        self.agent.activation_state = 'AWAITING_NUMBER'
        self.agent.activation_error = None
        self.assertEqual(self.agent.health()['status'], 'awaiting_number')
        self.agent.activation_state = 'READY'
        self.assertEqual(self.agent.health()['status'], 'ready')

    def test_registration_opens_viber_before_waiting_for_a_message_database(self):
        self.agent.activation_state = 'AWAITING_NUMBER'
        self.agent.activation_error = None
        with patch.object(Agent,'linked_profiles',side_effect=[[],[],[Mock()]]), patch('app.vm_agent.subprocess.Popen') as launch, patch('app.vm_agent.time.sleep'), patch.object(self.agent,'stop_viber') as stop, patch.object(self.agent,'_activate') as activate, patch.dict('os.environ',{'LOCALAPPDATA':'C:/Users/ViberWorker/AppData/Local'}):
            self.agent._await_activation()
        launch.assert_called_once()
        stop.assert_called_once()
        activate.assert_called_once_with(60)

    def test_linked_profile_skips_registration_and_stops_before_capture(self):
        calls = []
        with patch.object(Agent,'linked_profiles',return_value=[Mock()]), patch('app.vm_agent.subprocess.Popen') as launch, patch.object(self.agent,'stop_viber',side_effect=lambda _:calls.append('stopped')), patch.object(self.agent,'_activate',side_effect=lambda _:calls.append('capture')), patch.dict('os.environ',{'LOCALAPPDATA':'C:/Users/ViberWorker/AppData/Local'}):
            self.agent._await_activation()
        launch.assert_not_called()
        self.assertEqual(calls,['stopped','capture'])

    def test_capture_is_blocked_until_previous_viber_process_has_stopped(self):
        with patch.object(Agent,'linked_profiles',return_value=[Mock()]), patch.object(self.agent,'stop_viber',side_effect=RuntimeError('still running')), patch.object(self.agent,'_activate') as activate, patch.dict('os.environ',{'LOCALAPPDATA':'C:/Users/ViberWorker/AppData/Local'}):
            self.agent._await_activation()
        activate.assert_not_called()
        self.assertEqual(self.agent.activation_state,'ERROR')
        self.assertEqual(self.agent.activation_error,'still running')


if __name__ == '__main__': unittest.main()
