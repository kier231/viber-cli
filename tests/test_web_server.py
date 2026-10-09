from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

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

    def test_database_inbox_requires_session_and_never_submits_desktop_work(self):
        self.assertEqual(self.request('/viber/api/database/status')[0], 403)
        token = self.session()
        headers = {'X-Viber-CSRF': token}
        status, _, body = self.request('/viber/api/database/status', headers=headers)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['state'], 'STOPPED')
        self.assertFalse(result['automatic_replies'])
        status, _, body = self.request('/viber/api/database/conversations', headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])
        bad = {'source_id': 'missing', 'chat_id': 10, 'enabled': 'true'}
        status, _, _ = self.request('/viber/api/database/monitor', 'POST', json.dumps(bad),
                                   {**headers, 'Origin': self.origin, 'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        self.assertEqual(self.server.service.pending, 0)

    def test_campaign_http_draft_review_activation_and_controls(self):
        lead = self.server.service.store.create_with_android('+381641234567', 'Business', lambda *_: None)
        headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Viber-CSRF': self.session()}
        def post(action, payload, expected=200):
            status, _, body = self.request('/viber/api/' + action, 'POST', json.dumps(payload), headers)
            self.assertEqual(status, expected, body)
            return json.loads(body)
        draft = post('campaigns', {'request_key': str(uuid.uuid4()), 'name': 'HTTP campaign', 'lead_ids': [lead.id],
                                  'text': 'Hello {{company}}', 'schedule': {'mode': 'delay', 'minutes': 60}}, 201)
        self.assertEqual(draft['state'], 'DRAFT')
        review = post('campaign-review', {'campaign_id': draft['id']})
        active = post('campaign-activate', {'preview_token': review['token'], 'confirmed': True, 'request_key': str(uuid.uuid4())})
        self.assertEqual(active['counts'], {'SCHEDULED': 1})
        self.assertEqual(post('campaign-pause', {'campaign_id': draft['id']})['state'], 'PAUSED')
        self.assertEqual(post('campaign-cancel', {'campaign_id': draft['id']})['counts'], {'CANCELLED': 1})
        status, _, body = self.request('/viber/api/campaigns/' + draft['id'], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['state'], 'CANCELLED')
        self.assertEqual(self.server.service.pending, 0)


if __name__ == "__main__":
    unittest.main()
