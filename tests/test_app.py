import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class QuietHandler(app.Handler):
    def log_message(self, *_):
        pass


class Client:
    def __init__(self, base):
        self.base = base
        self.csrf = None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, method="GET", body=None, csrf=True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if csrf and self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def login(self, email, password):
        status, data = self.request("/api/login", "POST", {"email": email, "password": password})
        self.csrf = data.get("csrfToken")
        return status, data


class BookingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.db"
        app.reset_database(self.db)
        self.server = app.BookingServer(("127.0.0.1", 0), QuietHandler, self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def test_health_login_and_role_permissions(self):
        client = Client(self.base)
        self.assertEqual(client.request("/api/health")[0], 200)
        self.assertEqual(client.login("aarav@gls-demo.invalid", "Demo123!")[0], 200)
        status, data = client.request("/api/admin/bookings")
        self.assertEqual(status, 403)
        self.assertIn("administrator", data["error"])
        status, _ = client.request("/api/logout", "POST", {}, csrf=False)
        self.assertEqual(status, 403)

    def test_validation_and_cancel_releases_slot(self):
        client = Client(self.base); client.login("meera@gls-demo.invalid", "Demo123!")
        past = (date.today() - timedelta(days=1)).isoformat()
        status, _ = client.request("/api/bookings", "POST", {"itemId": 2, "date": past, "slot": "morning", "purpose": "Class work"})
        self.assertEqual(status, 400)
        future = (date.today() + timedelta(days=2)).isoformat()
        payload = {"itemId": 2, "date": future, "slot": "morning", "purpose": "Class work"}
        status, made = client.request("/api/bookings", "POST", payload)
        self.assertEqual(status, 201)
        self.assertEqual(client.request("/api/bookings/%d/cancel" % made["id"], "POST", {})[0], 200)
        self.assertEqual(client.request("/api/bookings", "POST", payload)[0], 201)
        bad = dict(payload, itemId=2.5, slot="afternoon")
        self.assertEqual(client.request("/api/bookings", "POST", bad)[0], 400)

    def test_admin_checkout_and_return_state_machine(self):
        student = Client(self.base); student.login("meera@gls-demo.invalid", "Demo123!")
        future = (date.today() + timedelta(days=3)).isoformat()
        status, made = student.request("/api/bookings", "POST", {"itemId": 3, "date": future, "slot": "afternoon", "purpose": "Photo assignment"})
        self.assertEqual(status, 201)
        admin = Client(self.base); admin.login("kavya@gls-demo.invalid", "Admin123!")
        self.assertEqual(admin.request("/api/admin/%d/checkout" % made["id"], "POST", {})[1]["status"], "checked_out")
        self.assertEqual(admin.request("/api/admin/%d/return" % made["id"], "POST", {})[1]["status"], "returned")
        self.assertEqual(admin.request("/api/admin/%d/return" % made["id"], "POST", {})[0], 409)

    def test_simultaneous_reservations_one_wins_one_conflicts(self):
        clients = [Client(self.base), Client(self.base)]
        clients[0].login("aarav@gls-demo.invalid", "Demo123!"); clients[1].login("meera@gls-demo.invalid", "Demo123!")
        future = (date.today() + timedelta(days=5)).isoformat()
        payload = {"itemId": 5, "date": future, "slot": "afternoon", "purpose": "Concurrent test"}
        barrier = threading.Barrier(2)
        def reserve(client):
            barrier.wait(timeout=2)
            return client.request("/api/bookings", "POST", payload)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(reserve, clients))
        self.assertEqual(statuses, [201, 409])

    def test_fixture_account_migration_preserves_ids_and_bookings(self):
        with app.connect(self.db) as conn:
            original = conn.execute("SELECT id FROM users WHERE email='aarav@gls-demo.invalid'").fetchone()[0]
            booking_ids = [row[0] for row in conn.execute("SELECT id FROM bookings ORDER BY id")]
            old_email, old_name, _, _ = app.DEMO_ACCOUNT_MIGRATIONS[0]
            conn.execute(
                "UPDATE users SET name=?,email=? WHERE id=?",
                (old_name, old_email, original),
            )
        app.ensure_database(self.db)
        with app.connect(self.db) as conn:
            migrated = conn.execute("SELECT id,name FROM users WHERE email='aarav@gls-demo.invalid'").fetchone()
            after_booking_ids = [row[0] for row in conn.execute("SELECT id FROM bookings ORDER BY id")]
        self.assertEqual((migrated["id"], migrated["name"]), (original, "Aarav Shah"))
        self.assertEqual(after_booking_ids, booking_ids)


if __name__ == "__main__":
    unittest.main()
