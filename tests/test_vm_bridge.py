import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.vm_bridge import load_vm_config, VmViberClient
from app.viber import ViberError


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


if __name__ == "__main__":
    unittest.main()
