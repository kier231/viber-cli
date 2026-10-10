"""Authenticated HTTP bridge between the host app and its Viber VM."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from app.models import Message
from app.viber import ViberError
from app.reply_errors import BridgeUnavailable, ViberRetryable
import uuid


CONFIG_PATH = Path(os.environ.get(
    "VIBER_CLI_VM_CONFIG",
    str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "viber-cli" / "vm-bridge.json")))


def load_vm_config(path=None):
    if path is None:
        path = CONFIG_PATH
        # A controller launched outside packaged Codex cannot see its redirected
        # LocalAppData path. Reuse the same user's physical saved configuration.
        if not path.is_file() and not os.environ.get('VIBER_CLI_VM_CONFIG'):
            packages = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Packages'
            candidates = [folder / 'LocalCache/Local/viber-cli/vm-bridge.json'
                          for folder in packages.glob('OpenAI.Codex_*')
                          if (folder / 'LocalCache/Local/viber-cli/vm-bridge.json').is_file()]
            if len(candidates) > 1:
                raise ViberError('Multiple saved Codex VM configurations exist. Set VIBER_CLI_VM_CONFIG to the intended file.')
            if candidates:
                path = candidates[0]
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
            raise BridgeUnavailable(f"The Viber VM bridge is unavailable: {exc}") from None
        if not isinstance(result,dict) or result.get('outcome_unknown') is True:
            raise BridgeUnavailable('The Viber VM response did not confirm the desktop operation outcome.')
        if not result.get("ok"):
            if result.get('retryable') is True and isinstance(result.get('error_code'), str):
                raise ViberRetryable(result.get('error', 'Viber is not ready.'), result['error_code'])
            raise ViberError(result.get("error", "The Viber VM operation failed."))
        if not isinstance(result.get('result'),dict):
            raise BridgeUnavailable('The Viber VM returned an invalid operation result.')
        return result['result']


class VmWorkerSource:
    def __init__(self, bridge, native_source_id=None, source_id=None):
        self.bridge = bridge
        if bool(native_source_id) != bool(source_id):
            raise ViberError('A history baseline needs both native and controller source identities.')
        self.native_source_id, self.source_id = native_source_id, source_id

    def read(self, request, timeout=70):
        if self.source_id:
            # Old native event IDs can be reused after an owner-confirmed reset.
            # Only the new epoch's checkpoint may be sent to this native reader.
            request = {**request, 'checkpoints': {self.native_source_id:
                request.get('checkpoints', {}).get(self.source_id, 0)}}
        result = self.bridge.call("/source/read", request, timeout=timeout)
        if self.source_id:
            if result.get('source_id') != self.native_source_id:
                raise ViberError('Viber profile changed after its history baseline was verified.')
            result = {**result, 'source_id': self.source_id}
        return result

    def close(self):
        return None


class VmViberClient:
    def __init__(self, bridge):
        self.bridge = bridge
        self.expected_name = None
        self.prepare_key = None

    def connect(self):
        try:
            self.bridge.call("/health", timeout=10)
        except BridgeUnavailable as exc:
            raise ViberRetryable(str(exc), 'worker_unavailable') from exc
        return self

    def open_phone(self, phone):
        try:
            self.expected_name = self.bridge.call("/desktop/open", {"phone": phone})["name"]
        except BridgeUnavailable as exc:
            raise ViberRetryable(str(exc), 'recipient_open_unavailable') from exc
        return self.expected_name

    def recover_pending(self, request_id):
        try:
            result = self.bridge.call('/desktop/cancel-request', {'request_id': request_id})
        except BridgeUnavailable as exc:
            raise ViberRetryable(str(exc), 'draft_cleanup_unavailable') from exc
        if result.get('clean') is not True:
            raise ViberError('The previous unsent draft could not be verified as cleared.')

    def current_name(self):
        return self.bridge.call("/desktop/current-name")["name"]

    def verify_current_name(self, expected):
        try:
            return bool(self.bridge.call("/desktop/verify", {"name": expected})["verified"])
        except BridgeUnavailable as exc:
            raise ViberRetryable(str(exc), 'header_verification_unavailable') from exc

    def read_messages(self):
        return [Message(**item) for item in self.bridge.call("/desktop/messages")["messages"]]

    def send_message(self, text, before_dispatch=None, on_dispatch=None, dispatch_lock=None):
        request_id = self.prepare_key or str(uuid.uuid4())
        try:
            draft = self.bridge.call("/desktop/prepare", {"text": text, 'request_id':request_id})["draft"]
        except BridgeUnavailable as exc:
            # A lost prepare response may still have typed text. Queue cleanup
            # behind that same request and require an explicit empty-draft ack.
            self.recover_pending(request_id)
            raise ViberRetryable(str(exc), 'prepare_response_lost') from exc
        try:
            if before_dispatch:
                before_dispatch()
            from contextlib import nullcontext
            with dispatch_lock if dispatch_lock is not None else nullcontext():
                if on_dispatch:
                    on_dispatch()
                self.bridge.call("/desktop/dispatch", {"draft": draft})
        except Exception as exc:
            try:
                result = self.bridge.call("/desktop/cancel", {"draft": draft}, timeout=30)
                if result.get('cancelled') is not True:
                    raise ViberError('Viber did not confirm clearing the unsent draft.')
            except Exception as cleanup:
                raise ViberError(f'{exc} Draft cleanup was not confirmed: {cleanup}') from exc
            raise

