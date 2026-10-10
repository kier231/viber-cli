"""Viber VM agent. Run only in the interactive ViberWorker desktop session."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import secrets
import threading
import subprocess
import time
from pathlib import Path

from app.viber_background import BackgroundViberClient
from app.reply_errors import ReplyRetryable, ViberRetryable
from app.viber import ViberError
from app.viber_key_capture import start_viber_and_capture_key
from app.viber_watcher import WorkerSource


class Agent:
    def __init__(self, wait_for_activation=False):
        self.desktop = None
        self.desktop_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viber-ui")
        self.source_lock = threading.Lock()
        self.source = None
        self.draft = None
        self.draft_request_id = None
        self.draft_text = None
        self.prepare_outcomes = {}
        self.activation_state = 'AWAITING_NUMBER'
        self.activation_error = None
        if wait_for_activation:
            self.activation_thread = threading.Thread(target=self._await_activation, name='viber-activation', daemon=True)
            self.activation_thread.start()
        else:
            self._activate(45)

    @staticmethod
    def linked_profiles():
        from app.viber_database import international_phone
        return [p for p in (Path(os.environ['APPDATA'])/'ViberPC').glob('*/viber.db')
                if international_phone(p.parent.name) and p.is_file() and p.stat().st_size > 0]

    @staticmethod
    def stop_viber(executable):
        import psutil
        user = psutil.Process().username().casefold()
        processes = []
        for process in psutil.process_iter(['name','exe','username']):
            try:
                if (process.info['name'] or '').lower() == 'viber.exe' and (process.info['username'] or '').casefold() == user and process.info['exe'] and Path(process.info['exe']).resolve() == executable.resolve():
                    process.terminate()
                    processes.append(process)
            except psutil.NoSuchProcess:
                continue
        _, alive = psutil.wait_procs(processes,timeout=15)
        if alive:
            raise RuntimeError('Viber did not stop; key capture replacement is blocked.')

    def _await_activation(self):
        try:
            executable = Path(os.environ['LOCALAPPDATA'])/'Viber'/'Viber.exe'
            if not self.linked_profiles():
                # Registration has no message database yet. Open the normal
                # Viber registration UI first, then capture only after linking.
                subprocess.Popen([str(executable)],close_fds=True)
                while not self.linked_profiles():
                    time.sleep(2)
                time.sleep(3)
            self.stop_viber(executable)
            self._activate(60)
        except Exception as exc:
            self.activation_error = str(exc)
            self.activation_state = 'ERROR'

    def _activate(self, timeout):
        try:
            viber = Path(os.environ['LOCALAPPDATA']) / 'Viber' / 'Viber.exe'
            self.database_key, self.capture_session = start_viber_and_capture_key(viber, timeout=timeout)
            self.source = WorkerSource(self.database_key)
            self.activation_state = 'READY'
        except Exception as exc:
            self.activation_error = str(exc)
            self.activation_state = 'ERROR'
            if timeout is not None:
                raise

    def health(self):
        result = {'status': 'ready' if self.activation_state == 'READY' else 'awaiting_number',
                  'instance_id': os.environ.get('VIBER_CLI_VM_INSTANCE_ID')}
        if self.activation_error:
            result.update(status='error', error=self.activation_error)
        return result

    def ui(self, action, payload):
        if getattr(self, 'activation_state', 'READY') != 'READY':
            raise ValueError('Link the new Viber account before opening conversations or preparing messages.')
        def run():
            import pythoncom
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            try:
                if self.desktop is None:
                    # The VM is the isolation boundary, so Viber may own the
                    # guest foreground without taking focus on the host.
                    self.desktop = BackgroundViberClient(allow_foreground=True).connect()
                if action == "inspect":
                    return {"draft_pending": self.draft is not None,
                            "controls": [{"type": self.desktop.ui._type(n),
                                          "name": self.desktop.ui._name(n),
                                          "automation_id": self.desktop.ui._auto_id(n)}
                                         for n in self.desktop._nodes()]}
                if action == "open":
                    if self.draft is not None:
                        raise ValueError("A VM draft is pending. Review or cancel it before opening another chat.")
                    # Reacquire the main window after Viber/VM resume; do not
                    # keep navigating through an obsolete UIA wrapper.
                    self.desktop.connect()
                    return {"name": self.desktop.open_phone(payload["phone"])}
                if action == "current-name":
                    return {"name": self.desktop.current_name()}
                if action == "verify":
                    return {"verified": self.desktop.verify_current_name(payload["name"])}
                if action == "messages":
                    return {"messages": [asdict(item) for item in self.desktop.read_messages()]}
                if action == "prepare":
                    request_id = payload.get('request_id')
                    if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id)>200):
                        raise ValueError('Invalid prepare request identity.')
                    if self.draft is not None:
                        if request_id and request_id == getattr(self, 'draft_request_id', None):
                            if payload.get('text') != self.draft_text:
                                raise ViberError('Prepare request identity was reused with different text.')
                            return {'draft': self.draft}
                        raise ValueError("A VM draft is already pending.")
                    outcomes = getattr(self, 'prepare_outcomes', {})
                    if request_id and outcomes.get(request_id) == 'dispatched':
                        raise ViberError('This prepare request has already dispatched. Check delivery before retrying.')
                    self.desktop.prepare_message(payload["text"])
                    self.draft = secrets.token_urlsafe(24)
                    self.draft_request_id = request_id
                    self.draft_text = payload['text']
                    return {"draft": self.draft}
                if action == 'cancel-request':
                    request_id = payload.get('request_id')
                    if not isinstance(request_id, str) or not request_id or len(request_id)>200:
                        raise ValueError('Invalid prepare request identity.')
                    if getattr(self, 'prepare_outcomes', {}).get(request_id) == 'dispatched':
                        raise ViberError('This request already dispatched; cancellation cannot authorize another send.')
                    if self.draft is not None:
                        if request_id != getattr(self, 'draft_request_id', None):
                            raise ViberError('A different request owns the unsent Viber draft.')
                        self.desktop.cancel_prepared()
                    if self.desktop.prepared is not None or self.desktop.ui._composer().get_value():
                        raise ViberError('An unverified draft remains in Viber. Check it before retrying.')
                    self.draft = None
                    self.draft_request_id = None
                    self.draft_text = None
                    return {'clean': True}
                if not self.draft or not hmac.compare_digest(payload.get("draft", ""), self.draft):
                    raise ValueError("The VM draft token is invalid or expired.")
                if action == "dispatch":
                    self.desktop.dispatch_prepared()
                    request_id = getattr(self, 'draft_request_id', None)
                    if request_id:
                        if not hasattr(self, 'prepare_outcomes'):
                            self.prepare_outcomes = {}
                        self.prepare_outcomes[request_id] = 'dispatched'
                        if len(self.prepare_outcomes)>256:
                            del self.prepare_outcomes[next(iter(self.prepare_outcomes))]
                    self.draft = None
                    self.draft_request_id = None
                    self.draft_text = None
                    return {"dispatched": True}
                if action == "cancel":
                    self.desktop.cancel_prepared()
                    if self.desktop.prepared is not None or self.desktop.ui._composer().get_value():
                        raise ViberError('Viber draft cleanup was not confirmed; the draft remains unsent.')
                    self.draft = None
                    self.draft_request_id = None
                    self.draft_text = None
                    return {"cancelled": True}
                raise ValueError("Unknown desktop action.")
            finally:
                pythoncom.CoUninitialize()
        return self.desktop_pool.submit(run).result(timeout=70)

    def read(self, payload):
        if getattr(self, 'activation_state', 'READY') != 'READY':
            raise ValueError('The new Viber number has not been linked yet.')
        with self.source_lock:
            try:
                return self.source.read(payload)
            except Exception:
                self.source.close()
                self.source = WorkerSource(self.database_key)
                raise


def serve(host="0.0.0.0", port=4011):
    state_dir = Path(os.environ['LOCALAPPDATA'])/'viber-cli'
    instance_path = state_dir/'instance.json'
    if instance_path.is_file():
        instance = json.loads(instance_path.read_text(encoding='utf-8-sig'))
        if instance.get('id') not in ('instance-1','instance-2'):
            raise SystemExit('Unknown Viber instance.')
        os.environ['VIBER_CLI_VM_INSTANCE_ID'] = instance['id']
        os.environ['VIBER_CLI_AWAIT_ACTIVATION'] = '1'
        if not os.environ.get('VIBER_CLI_VM_TOKEN'):
            os.environ['VIBER_CLI_VM_TOKEN'] = (state_dir/'vm-token.txt').read_text(encoding='utf-8').strip()
        os.environ['PATH'] = 'C:\\Program Files\\Tesseract-OCR;' + os.environ.get('PATH','')
        os.environ['TESSDATA_PREFIX'] = str(state_dir/'tessdata')
    token = os.environ.get("VIBER_CLI_VM_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("Set VIBER_CLI_VM_TOKEN to a strong random value.")
    agent = Agent(wait_for_activation=os.environ.get('VIBER_CLI_AWAIT_ACTIVATION') == '1')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            try:
                supplied = self.headers.get("X-Viber-Bridge-Token", "")
                if not hmac.compare_digest(supplied, token):
                    self.send_error(403)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length > 20000:
                    raise ValueError("Request is too large.")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/health":
                    result = agent.health()
                elif self.path == "/source/read":
                    result = agent.read(payload)
                elif self.path.startswith("/desktop/"):
                    result = agent.ui(self.path.rsplit("/", 1)[1], payload)
                else:
                    self.send_error(404)
                    return
                response = {"ok": True, "result": result}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
                if isinstance(exc, TimeoutError) and self.path.startswith('/desktop/'):
                    response.update(error='The desktop request is still running; reconcile its result.', outcome_unknown=True)
                if isinstance(exc, (ViberRetryable, ReplyRetryable)):
                    response.update(retryable=True, error_code=exc.code)
            body = json.dumps(response, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve(port=int(os.environ.get("VIBER_CLI_VM_PORT", "4011")))
