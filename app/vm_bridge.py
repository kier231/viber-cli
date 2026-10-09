"""Authenticated HTTP bridge between the host app and its Viber VM."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from app.models import Message
from app.viber import ViberError


CONFIG_PATH = Path(os.environ.get(
    "VIBER_CLI_VM_CONFIG",
    str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "viber-cli" / "vm-bridge.json")))


def load_vm_config(path=CONFIG_PATH):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        url, token = value["url"].rstrip("/"), value["token"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ViberError(f"VM bridge configuration is invalid: {path}") from None
    if not url.startswith("http://127.0.0.1:") or not isinstance(token, str) or len(token) < 32:
        raise ViberError("VM bridge must use a loopback URL and a strong token.")
    return {"url": url, "token": token}


class VmBridge:
    def __init__(self, config):
        self.url, self.token = config["url"], config["token"]

    def call(self, path, payload=None, timeout=70):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Viber-Bridge-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise ViberError(f"The Viber VM bridge is unavailable: {exc}") from None
        if not result.get("ok"):
            raise ViberError(result.get("error", "The Viber VM operation failed."))
        return result.get("result")


class VmWorkerSource:
    def __init__(self, bridge):
        self.bridge = bridge

    def read(self, request):
        return self.bridge.call("/source/read", request)

    def close(self):
        return None


class VmViberClient:
    def __init__(self, bridge):
        self.bridge = bridge
        self.expected_name = None

    def connect(self):
        self.bridge.call("/health", timeout=10)
        return self

    def open_phone(self, phone):
        self.expected_name = self.bridge.call("/desktop/open", {"phone": phone})["name"]
        return self.expected_name

    def current_name(self):
        return self.bridge.call("/desktop/current-name")["name"]

    def verify_current_name(self, expected):
        return bool(self.bridge.call("/desktop/verify", {"name": expected})["verified"])

    def read_messages(self):
        return [Message(**item) for item in self.bridge.call("/desktop/messages")["messages"]]

    def send_message(self, text, before_dispatch=None, on_dispatch=None, dispatch_lock=None):
        draft = self.bridge.call("/desktop/prepare", {"text": text})["draft"]
        try:
            if before_dispatch:
                before_dispatch()
            from contextlib import nullcontext
            with dispatch_lock if dispatch_lock is not None else nullcontext():
                if on_dispatch:
                    on_dispatch()
                self.bridge.call("/desktop/dispatch", {"draft": draft})
        except Exception:
            try:
                self.bridge.call("/desktop/cancel", {"draft": draft}, timeout=15)
            except Exception:
                pass
            raise

