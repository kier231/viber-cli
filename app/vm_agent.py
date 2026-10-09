"""Viber VM agent. Run only in the interactive ViberWorker desktop session."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import secrets
import threading

from app.viber_background import BackgroundViberClient
from app.viber_watcher import WorkerSource


class Agent:
    def __init__(self):
        self.desktop = None
        self.desktop_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viber-ui")
        self.source = WorkerSource()
        self.source_lock = threading.Lock()
        self.draft = None

    def ui(self, action, payload):
        def run():
            import pythoncom
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            try:
                if self.desktop is None:
                    self.desktop = BackgroundViberClient().connect()
                if action == "open":
                    return {"name": self.desktop.open_phone(payload["phone"])}
                if action == "current-name":
                    return {"name": self.desktop.current_name()}
                if action == "verify":
                    return {"verified": self.desktop.verify_current_name(payload["name"])}
                if action == "messages":
                    return {"messages": [asdict(item) for item in self.desktop.read_messages()]}
                if action == "prepare":
                    if self.draft is not None:
                        raise ValueError("A VM draft is already pending.")
                    self.desktop.prepare_message(payload["text"])
                    self.draft = secrets.token_urlsafe(24)
                    return {"draft": self.draft}
                if not self.draft or not hmac.compare_digest(payload.get("draft", ""), self.draft):
                    raise ValueError("The VM draft token is invalid or expired.")
                if action == "dispatch":
                    self.desktop.dispatch_prepared()
                    self.draft = None
                    return {"dispatched": True}
                if action == "cancel":
                    self.desktop.cancel_prepared()
                    self.draft = None
                    return {"cancelled": True}
                raise ValueError("Unknown desktop action.")
            finally:
                pythoncom.CoUninitialize()
        return self.desktop_pool.submit(run).result(timeout=70)

    def read(self, payload):
        with self.source_lock:
            return self.source.read(payload)


def serve(host="0.0.0.0", port=4011):
    token = os.environ.get("VIBER_CLI_VM_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("Set VIBER_CLI_VM_TOKEN to a strong random value.")
    agent = Agent()

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
                    result = {"status": "ready"}
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
            body = json.dumps(response, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve(port=int(os.environ.get("VIBER_CLI_VM_PORT", "4011")))
