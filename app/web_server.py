"""Loopback-only gateway: original email UI plus the local Viber workspace."""

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
import time
from urllib.parse import parse_qs, urlsplit

from app.web_service import WebService


ASSETS = Path(__file__).resolve().parent.parent / "web"
SWITCHER = """<div class="workspace-picker">
<h1><button type="button" id="workspace-switch" aria-expanded="false" aria-controls="workspace-menu">{name}<span aria-hidden="true"> ▾</span></button></h1>
<div id="workspace-menu" hidden><a href="/" {email_current}>EmailOutreach</a><a href="/viber" {viber_current}>ViberOutreach</a></div></div>"""
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def switcher(viber=False):
    return SWITCHER.format(name="ViberOutreach" if viber else "EmailOutreach",
                           email_current='' if viber else 'aria-current="page"',
                           viber_current='aria-current="page"' if viber else '')


def decorate_email(html):
    marker = "<h1>EmailOutreach</h1>"
    if marker not in html:
        raise ValueError("The email page layout changed. Update the workspace switcher selector.")
    html = html.replace(marker, switcher(), 1)
    return html.replace("</head>", '<link rel="stylesheet" href="/outreach/switch.css">'
                        '<script src="/outreach/switch.js" defer></script></head>', 1)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, email_port, service):
        if port == email_port:
            raise ValueError("The email service and the new app must use different ports.")
        self.email_port = email_port
        self.service = service
        self.sessions = {}
        super().__init__(("127.0.0.1", port), Handler)

    def server_close(self):
        super().server_close()
        self.service.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        # Contact data, message text and confirmation tokens stay out of console logs.
        pass

    def _reply(self, status, body, content_type="application/json; charset=utf-8", extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _check_request(self):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if len(self.headers.get_all("Host", [])) != 1 or host not in hosts:
            raise PermissionError("Use this app's localhost address.")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise PermissionError("Cross-origin requests are blocked.")
        if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
            raise PermissionError("Cross-site requests are blocked.")
        if self.command not in {"GET", "HEAD"} and origin != f"http://{host}":
            raise PermissionError("Open the localhost app to perform this action.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("Invalid local request path.")
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            raise ValueError("Unsupported request framing.")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1048576:
            raise ValueError("Requests must be smaller than 1 MB.")
        self.request_body = self.rfile.read(length) if length else b""
        if len(self.request_body) != length:
            raise ValueError("Incomplete request body.")
        return parsed

    def _json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send JSON from the localhost app.")
        payload = json.loads(self.request_body)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return payload

    def _authenticate(self):
        token = self.headers.get("X-Viber-CSRF", "")
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise PermissionError("Reload ViberOutreach to reconnect.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.server.service.lock:
            expiry = self.server.sessions.get(digest, 0)
        if expiry <= time.time():
            raise PermissionError("Your local session expired. Reload ViberOutreach.")

    def _viber_api(self, path, query):
        service = self.server.service
        if path == "/viber/api/session" and self.command == "POST":
            payload = self._json()
            if payload or self.headers.get("X-Viber-Browser") != "1":
                raise PermissionError("Open the ViberOutreach page to connect.")
            token = secrets.token_hex(32)
            with service.lock:
                self.server.sessions = {key: value for key, value in self.server.sessions.items() if value > time.time()}
                if len(self.server.sessions) >= 128:
                    raise ValueError("Too many open sessions. Restart the local app.")
                self.server.sessions[hashlib.sha256(token.encode()).hexdigest()] = time.time() + 43200
            return self._reply(200, {"csrf": token})
        self._authenticate()
        if self.command == "GET":
            if path == "/viber/api/database/status":
                return self._reply(200, {**service.watcher.status(), 'automatic_replies': service.replies.settings()['enabled']})
            if path == "/viber/api/replies":
                return self._reply(200, {'settings': service.replies.settings(), 'jobs': service.replies.jobs()})
            if path == "/viber/api/database/conversations":
                return self._reply(200, service.watcher.inbox.conversations(
                    offset=int(query.get('offset', ['0'])[0])))
            if path == "/viber/api/database/conversation":
                before = query.get('before', [None])[0]
                result = service.watcher.inbox.conversation(
                    query.get('source_id', [''])[0], int(query.get('chat_id', ['0'])[0]),
                    before=int(before) if before is not None else None)
                result['automatic_replies'] = bool(service.replies.settings()['enabled'] and result['conversation']['reply_enabled'] and result['conversation']['monitoring'])
                return self._reply(200, result)
            if path == "/viber/api/contacts":
                return self._reply(200, service.contacts())
            if path == "/viber/api/campaigns":
                return self._reply(200, service.campaigns.list())
            if path.startswith("/viber/api/campaigns/"):
                return self._reply(200, service.campaigns.detail(path.rsplit("/", 1)[1]))
            if path == "/viber/api/status":
                return self._reply(200, {"pending": service.pending, "contacts": len(service.store.all())})
            if path.startswith("/viber/api/operations/"):
                return self._reply(200, service.operation(path.rsplit("/", 1)[1]))
            if path in {"/viber/api/sent", "/viber/api/scheduled", "/viber/api/events", "/viber/api/inbox"}:
                offset = int(query.get("offset", ["0"])[0])
                if offset < 0:
                    raise ValueError("Invalid page offset.")
                return self._reply(200, service.records(path.rsplit("/", 1)[1], offset=offset))
            if path == "/viber/api/activity":
                return self._reply(200, service.activity(int(query.get("days", ["30"])[0])))
        if self.command == "POST":
            payload = self._json()
            if path == "/viber/api/replies":
                return self._reply(200, service.replies.configure(payload.get('enabled'), payload.get('instructions')))
            if path == "/viber/api/replies/conversation":
                return self._reply(200, service.replies.conversation_control(payload.get('source_id'), payload.get('chat_id'), payload.get('enabled')))
            if path == "/viber/api/database/monitor":
                return self._reply(200, service.watcher.inbox.monitor(
                    payload.get('source_id'), payload.get('chat_id'), payload.get('enabled')))
            if path == "/viber/api/contacts":
                result = service.add_contact(payload)
            elif path == "/viber/api/campaigns":
                return self._reply(201, service.campaigns.create(payload))
            elif path == "/viber/api/campaign-review":
                return self._reply(200, service.campaigns.review(payload.get("campaign_id")))
            elif path == "/viber/api/campaign-activate":
                return self._reply(200, service.campaigns.activate(payload))
            elif path in {"/viber/api/campaign-pause", "/viber/api/campaign-cancel"}:
                return self._reply(200, service.campaigns.control(payload.get("campaign_id"), path.rsplit("-", 1)[1]))
            elif path == "/viber/api/name":
                return self._reply(200, service.set_name(payload.get("lead_id"), payload.get("name")))
            elif path in {"/viber/api/open", "/viber/api/read"}:
                result = service.open_contact(payload.get("lead_id"), read=path.endswith("/read"))
            elif path == "/viber/api/read-current":
                result = service.read_current()
            elif path == "/viber/api/prepare":
                result = service.prepare(payload.get("lead_id"), payload.get("text"), payload.get("schedule"))
            elif path == "/viber/api/send":
                result = service.send(payload)
            elif path == "/viber/api/cancel-scheduled":
                return self._reply(200, service.cancel_scheduled(payload.get("send_id")))
            elif path in {"/viber/api/diagnostics", "/viber/api/inspect"}:
                result = service.diagnostics(inspect=path.endswith("/inspect"))
            else:
                return self._reply(404, {"message": "Viber action not found."})
            return self._reply(202, result)
        return self._reply(404, {"message": "Viber endpoint not found."})

    def _proxy_email(self, path):
        upstream_origin = f"http://127.0.0.1:{self.server.email_port}"
        dropped = {"host", "origin", "referer", "connection", "content-length", "accept-encoding",
                   "cookie", "transfer-encoding", "upgrade", "proxy-authorization", "proxy-connection"}
        headers = {key: value for key, value in self.headers.items() if key.lower() not in dropped}
        headers["Host"] = f"127.0.0.1:{self.server.email_port}"
        if self.headers.get("Origin"):
            headers["Origin"] = upstream_origin
        if self.headers.get("Referer"):
            headers["Referer"] = upstream_origin + "/"
        cookies = [part.strip() for part in self.headers.get("Cookie", "").split(";")
                   if part.strip().startswith("viber_email_browser=")]
        if len(cookies) == 1:
            value = cookies[0].split("=", 1)[1]
            if re.fullmatch(r"[a-f0-9]{64}", value):
                headers["Cookie"] = "mailbox_browser=" + value
        connection = http.client.HTTPConnection("127.0.0.1", self.server.email_port, timeout=95)
        try:
            connection.request(self.command, self.path, body=self.request_body or None, headers=headers)
            response = connection.getresponse()
            body = response.read()
            if path == "/" and response.status == 200 and self.command != "HEAD":
                body = decorate_email(body.decode("utf-8")).encode("utf-8")
            extra = []
            ignored = {"content-length", "content-type", "connection", "transfer-encoding", "content-encoding",
                       "cache-control", "content-security-policy", "x-content-type-options", "x-frame-options",
                       "referrer-policy", "cross-origin-resource-policy", "etag", "last-modified"}
            for key, value in response.getheaders():
                if key.lower() in ignored:
                    continue
                if key.lower() == "set-cookie":
                    if not value.startswith("mailbox_browser="):
                        continue
                    value = value.replace("mailbox_browser=", "viber_email_browser=", 1)
                if key.lower() == "location":
                    value = value.replace(upstream_origin + "/", "/", 1)
                extra.append((key, value))
            self._reply(response.status, body, response.getheader("Content-Type", "application/json"), extra)
        except (OSError, http.client.HTTPException):
            if path == "/" and self.command in {"GET", "HEAD"}:
                page = (ASSETS / "email-offline.html").read_text(encoding="utf-8").replace("{{switcher}}", switcher())
                self._reply(503, page, "text/html; charset=utf-8")
            else:
                self._reply(502, {"message": "EmailOutreach on port 4000 is unavailable. Keep its Docker service running."})
        finally:
            connection.close()

    def _handle(self):
        try:
            parsed = self._check_request()
            path = parsed.path
            if path.startswith("/viber/api/"):
                return self._viber_api(path, parse_qs(parsed.query))
            if self.command in {"GET", "HEAD"}:
                if path in {"/viber", "/viber/"}:
                    page = (ASSETS / "viber.html").read_text(encoding="utf-8").replace("{{switcher}}", switcher(True))
                    return self._reply(200, page, "text/html; charset=utf-8")
                assets = {"/outreach/switch.js": ("switch.js", "text/javascript; charset=utf-8"),
                          "/outreach/switch.css": ("switch.css", "text/css; charset=utf-8"),
                          "/outreach/email.css": ("email.css", "text/css; charset=utf-8"),
                          "/outreach/viber.js": ("viber.js", "text/javascript; charset=utf-8")}
                if path in assets:
                    filename, mime = assets[path]
                    return self._reply(200, (ASSETS / filename).read_bytes(), mime)
            return self._proxy_email(path)
        except PermissionError as exc:
            self.close_connection = True
            self._reply(403, {"message": str(exc)})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.close_connection = True
            self._reply(400, {"message": str(exc)})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self._reply(500, {"message": "The local request failed. Check ViberOutreach's event log and review Sent before retrying."})

    do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = _handle
