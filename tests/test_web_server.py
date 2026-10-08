from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app.web_server import LocalServer
from app.web_service import WebService


class FakeEmail(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.seen.append((self.command, self.path, dict(self.headers)))
        body = b'<html><head></head><body><h1>EmailOutreach</h1><nav>Compose Scheduled Contacts Campaigns Daily activity</nav></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.seen.append((self.command, self.path, dict(self.headers)))
        body = b'{"csrf":"upstream-token","mailboxes":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "mailbox_browser=" + "a" * 64 + "; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        FakeEmail.seen.clear()
        self.email = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmail)
        self.server = LocalServer(0, self.email.server_address[1], WebService(Path(self.temp.name) / "test.db"))
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (self.email, self.server)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        for server in (self.server, self.email):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(2)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def session(self):
        status, _, body = self.request("/viber/api/session", "POST", "{}", {"Origin": self.origin, "Content-Type": "application/json", "X-Viber-Browser": "1"})
        self.assertEqual(status, 200)
        return json.loads(body)["csrf"]

    def test_email_page_adds_dropdown_and_keeps_original_content(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="workspace-switch"', body)
        self.assertIn(b'ViberOutreach', body)
        self.assertIn(b'Compose Scheduled Contacts Campaigns Daily activity', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_email_proxy_preserves_csrf_and_isolates_cookie(self):
        status, headers, _ = self.request("/browser/session", "POST", "{}", {
            "Origin": self.origin, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin",
            "X-Mailbox-Browser": "1", "X-Mailbox-CSRF": "original-csrf",
            "Cookie": "mailbox_browser=" + "b" * 64 + "; viber_email_browser=" + "a" * 64})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Set-Cookie"].startswith("viber_email_browser="))
        seen = FakeEmail.seen[-1][2]
        self.assertEqual(seen["Origin"], f"http://127.0.0.1:{self.email.server_address[1]}")
        self.assertEqual(seen["X-Mailbox-CSRF"], "original-csrf")
        self.assertEqual(seen["Cookie"], "mailbox_browser=" + "a" * 64)

    def test_cross_origin_or_bad_host_cannot_proxy_mutation(self):
        for headers in ({"Origin": "https://attacker.invalid"}, {"Host": "attacker.invalid", "Origin": self.origin},
                        {"Origin": self.origin, "Sec-Fetch-Site": "cross-site"}, {}):
            self.assertEqual(self.request("/emails", "POST", "{}", headers)[0], 403)
        self.assertEqual(FakeEmail.seen, [])

    def test_viber_apis_require_local_session_token(self):
        self.assertEqual(self.request("/viber/api/contacts")[0], 403)
        token = self.session()
        status, _, body = self.request("/viber/api/contacts", headers={"X-Viber-CSRF": token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_switcher_and_viber_page_load_without_email_service(self):
        self.email.shutdown()
        self.email.server_close()
        status, _, body = self.request("/viber")
        self.assertEqual(status, 200)
        self.assertIn(b'Send a Viber message', body)
        status, _, body = self.request("/")
        self.assertEqual(status, 503)
        self.assertIn(b'workspace-switch', body)
        self.assertIn(b'Email service is offline', body)


if __name__ == "__main__":
    unittest.main()
