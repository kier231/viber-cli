import json
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import unittest

from app.vm_bridge import load_vm_config, VmViberClient
from app.viber import ViberError
from app.viber_key_capture import valid_key
from app.viber_watcher import WorkerSource


class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, path, payload=None, timeout=70):
        self.calls.append((path, payload))
        if path == "/desktop/prepare":
            return {"draft": "one-time-draft"}
        return {"dispatched": True}


class VmBridgeTests(unittest.TestCase):
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
            ("/desktop/prepare", {"text": "Hello"}),
            ("/desktop/dispatch", {"draft": "one-time-draft"}),
        ])

    def test_failed_host_guard_cancels_verified_draft(self):
        bridge = FakeBridge()
        client = VmViberClient(bridge)
        with self.assertRaisesRegex(ValueError, "changed"):
            client.send_message("Hello", before_dispatch=lambda: (_ for _ in ()).throw(ValueError("changed")))
        self.assertEqual(bridge.calls[-1],
                         ("/desktop/cancel", {"draft": "one-time-draft"}))

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
