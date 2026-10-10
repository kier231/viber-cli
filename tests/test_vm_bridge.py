import json
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app.vm_bridge import load_vm_config, VmBridge, VmViberClient, VmWorkerSource
from io import BytesIO
from app.viber import ViberError
from app.reply_errors import BridgeUnavailable, ReplyRetryable, ViberRetryable
from app.viber_key_capture import valid_key
from app.viber_watcher import WorkerSource


class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, path, payload=None, timeout=70):
        self.calls.append((path, payload))
        if path == "/desktop/prepare":
            return {"draft": "one-time-draft"}
        if path == '/desktop/cancel':
            return {'cancelled':True}
        if path == '/desktop/cancel-request':
            return {'clean':True}
        return {"dispatched": True}


class VmBridgeTests(unittest.TestCase):
    def test_fresh_history_epoch_does_not_reuse_archived_checkpoint(self):
        bridge = Mock()
        bridge.call.return_value = {'source_id': 'native', 'max_event_id': 1}
        source = VmWorkerSource(bridge, 'native', 'fresh-epoch')
        request = {'phones': ['+381651234567'], 'checkpoints': {'native': 900, 'other': 700}}
        self.assertEqual(source.read(request)['source_id'], 'fresh-epoch')
        bridge.call.assert_called_once_with('/source/read', {
            'phones': ['+381651234567'], 'checkpoints': {'native': 0}}, timeout=70)
        self.assertEqual(request['checkpoints']['native'], 900)
        source.read({**request, 'checkpoints': {'native': 900, 'fresh-epoch': 12}})
        self.assertEqual(bridge.call.call_args.args[1]['checkpoints'], {'native': 12})

    def test_history_epoch_maps_cached_reads_and_rejects_another_profile(self):
        bridge = Mock()
        bridge.call.return_value = {'source_id': 'native', 'unchanged': True}
        source = VmWorkerSource(bridge, 'native', 'fresh-epoch')
        self.assertEqual(source.read({}), {'source_id': 'fresh-epoch', 'unchanged': True})
        bridge.call.return_value = {'source_id': 'another-profile', 'account_phone': '+381651234567'}
        with self.assertRaisesRegex(ViberError, 'profile changed'):
            source.read({})

    def test_history_epoch_requires_both_identities_and_preserves_ordinary_reads(self):
        bridge = Mock()
        for identities in (('native', None), (None, 'epoch')):
            with self.assertRaisesRegex(ViberError, 'both'):
                VmWorkerSource(bridge, *identities)
        bridge.call.return_value = {'source_id': 'native', 'max_event_id': 10}
        request = {'checkpoints': {'native': 10}}
        self.assertEqual(VmWorkerSource(bridge).read(request), bridge.call.return_value)
        bridge.call.assert_called_once_with('/source/read', request, timeout=70)

    def test_external_launcher_finds_packaged_codex_configuration(self):
        with TemporaryDirectory() as folder:
            local = Path(folder)
            config = local / 'Packages/OpenAI.Codex_test/LocalCache/Local/viber-cli/vm-bridge.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'url': 'http://127.0.0.1:4011', 'token': 'x' * 40}))
            with patch('app.vm_bridge.CONFIG_PATH', local / 'viber-cli/vm-bridge.json'), patch.dict('os.environ', {'LOCALAPPDATA': str(local), 'VIBER_CLI_VM_CONFIG': ''}):
                self.assertEqual(load_vm_config()['url'], 'http://127.0.0.1:4011')

    def test_explicit_missing_configuration_does_not_use_another_account(self):
        with TemporaryDirectory() as folder:
            local = Path(folder)
            config = local / 'Packages/OpenAI.Codex_test/LocalCache/Local/viber-cli/vm-bridge.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'url': 'http://127.0.0.1:4011', 'token': 'x' * 40}))
            with patch.dict('os.environ', {'LOCALAPPDATA': str(local), 'VIBER_CLI_VM_CONFIG': ''}):
                self.assertIsNone(load_vm_config(local / 'missing.json'))
            with patch('app.vm_bridge.CONFIG_PATH', local / 'missing.json'), patch.dict('os.environ', {'LOCALAPPDATA': str(local), 'VIBER_CLI_VM_CONFIG': str(local / 'missing.json')}):
                self.assertIsNone(load_vm_config())

    def test_ambiguous_packaged_configurations_require_an_explicit_choice(self):
        with TemporaryDirectory() as folder:
            local = Path(folder)
            for package in ('OpenAI.Codex_one', 'OpenAI.Codex_two'):
                config = local / 'Packages' / package / 'LocalCache/Local/viber-cli/vm-bridge.json'
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({'url': 'http://127.0.0.1:4011', 'token': 'x' * 40}))
            with patch('app.vm_bridge.CONFIG_PATH', local / 'missing.json'), patch.dict('os.environ', {'LOCALAPPDATA': str(local), 'VIBER_CLI_VM_CONFIG': ''}):
                with self.assertRaisesRegex(ViberError, 'Multiple saved'):
                    load_vm_config()

    def test_configuration_requires_loopback_and_strong_token(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "vm.json"
            path.write_text(json.dumps({"url": "http://10.0.2.15:4011", "token": "x" * 40}))
            with self.assertRaisesRegex(ViberError, "loopback"):
                load_vm_config(path)

    def test_host_guards_run_between_prepare_and_dispatch(self):
        bridge = FakeBridge()
        client = VmViberClient(bridge)
        events = []
        client.send_message("Hello", before_dispatch=lambda: events.append("checked"),
                            on_dispatch=lambda: events.append("submitting"))
        self.assertEqual(events, ["checked", "submitting"])
        self.assertEqual(bridge.calls, [
            ("/desktop/prepare", {"text": "Hello",'request_id':bridge.calls[0][1]['request_id']}),
            ("/desktop/dispatch", {"draft": "one-time-draft"}),
        ])

    def test_failed_host_guard_cancels_verified_draft(self):
        bridge = FakeBridge()
        client = VmViberClient(bridge)
        with self.assertRaisesRegex(ValueError, "changed"):
            client.send_message("Hello", before_dispatch=lambda: (_ for _ in ()).throw(ValueError("changed")))
        self.assertEqual(bridge.calls[-1],
                         ("/desktop/cancel", {"draft": "one-time-draft"}))

    def test_lost_prepare_response_requires_owned_cleanup_before_retry(self):
        bridge=Mock()
        bridge.call.side_effect=[BridgeUnavailable('Response lost'),{'clean':True}]
        client=VmViberClient(bridge)
        client.prepare_key='durable-request'
        with self.assertRaises(ViberRetryable):
            client.send_message('Hello')
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list],['/desktop/prepare','/desktop/cancel-request'])
        self.assertEqual(bridge.call.call_args.args[1],{'request_id':'durable-request'})

    def test_unconfirmed_cleanup_never_authorizes_a_retry(self):
        bridge=Mock()
        bridge.call.side_effect=[{'draft':'token'},ViberError('Cannot clear draft')]
        client=VmViberClient(bridge)
        with self.assertRaisesRegex(ViberError,'cleanup was not confirmed'):
            client.send_message('Hello',before_dispatch=lambda:(_ for _ in ()).throw(ReplyRetryable('Inbox unavailable')))
        self.assertNotIn('/desktop/dispatch',[c.args[0] for c in bridge.call.call_args_list])

    def test_worker_timeout_response_requires_reconciliation(self):
        response=BytesIO(json.dumps({'ok':False,'error':'Still running','outcome_unknown':True}).encode())
        bridge=VmBridge({'url':'http://127.0.0.1:4012','token':'x'*40})
        with patch('app.vm_bridge.urllib.request.urlopen',return_value=response):
            with self.assertRaises(BridgeUnavailable):
                bridge.call('/desktop/prepare',{'text':'Hello'})

    def test_database_key_is_forwarded_only_until_reader_accepts_it(self):
        class Input:
            def __init__(self):
                self.lines = []
            def write(self, line):
                self.lines.append(json.loads(line))
            def flush(self):
                pass

        source = WorkerSource.__new__(WorkerSource)
        source.database_key = 'ab' * 32
        source.process = type('Process', (), {'stdin': Input()})()
        source.responses = queue.Queue()
        source.responses.put({'ok': True, 'result': {'source_id': 'one'}})
        source.responses.put({'ok': True, 'result': {'source_id': 'one', 'unchanged': True}})
        source.read({'phones': [], 'checkpoints': {}})
        source.read({'phones': [], 'checkpoints': {}})
        self.assertIn('_database_key', source.process.stdin.lines[0])
        self.assertNotIn('_database_key', source.process.stdin.lines[1])

    def test_vm_key_shape_is_strict(self):
        self.assertTrue(valid_key('ab' * 32))
        self.assertFalse(valid_key('not-a-key'))
        self.assertFalse(valid_key('a' * 63))


if __name__ == "__main__":
    unittest.main()
