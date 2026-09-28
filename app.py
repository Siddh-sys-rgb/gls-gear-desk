#!/usr/bin/env python3
"""Dependency-free GLS Gear Desk portfolio demo."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import signal
import sqlite3
import sys
import threading
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "equipment.db"
STATIC = ROOT / "static"
SLOTS = ("morning", "afternoon")
DEMO_ACCOUNT_MIGRATIONS = (
    ("alice@college.edu", "Alice Johnson", "Aarav Shah", "aarav@gls-demo.invalid"),
    ("bob@college.edu", "Bob Martinez", "Meera Patel", "meera@gls-demo.invalid"),
    ("admin@college.edu", "Morgan Lee", "Kavya Desai", "kavya@gls-demo.invalid"),
)
SESSIONS: dict[str, dict] = {}
SESSION_LOCK = threading.Lock()
SESSION_MAX_AGE = 8 * 60 * 60


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return f"pbkdf2_sha256$210000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('student','admin'))
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN ('Camera','Projector')),
  description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  booking_date TEXT NOT NULL,
  slot TEXT NOT NULL CHECK(slot IN ('morning','afternoon')),
  purpose TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('reserved','cancelled','checked_out','returned')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking
  ON bookings(item_id, booking_date, slot)
  WHERE status IN ('reserved','checked_out');
CREATE INDEX IF NOT EXISTS bookings_user ON bookings(user_id, booking_date);
"""


class ClosingConnection(sqlite3.Connection):
    """Make `with connect(...)` close as well as commit or roll back."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=5, isolation_level=None, factory=ClosingConnection
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def reset_database(db_path: Union[str, Path] = DEFAULT_DB) -> None:
    db_path = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        users = [
            ("Aarav Shah", "aarav@gls-demo.invalid", "Demo123!", "student"),
            ("Meera Patel", "meera@gls-demo.invalid", "Demo123!", "student"),
            ("Kavya Desai", "kavya@gls-demo.invalid", "Admin123!", "admin"),
        ]
        conn.executemany(
            "INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
            [(name, email, hash_password(password), role) for name, email, password, role in users],
        )
        conn.executemany(
            "INSERT INTO items(name,category,description) VALUES(?,?,?)",
            [
                ("Canon EOS R50", "Camera", "Mirrorless camera · 24 MP · 4K video"),
                ("Sony Alpha a6400", "Camera", "Mirrorless camera · fast autofocus"),
                ("Nikon Z30", "Camera", "Compact creator camera · flip screen"),
                ("Epson PowerLite 118", "Projector", "Classroom projector · 3,800 lumens"),
                ("BenQ TH575", "Projector", "1080p projector · HDMI"),
            ],
        )
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        aarav = conn.execute("SELECT id FROM users WHERE email='aarav@gls-demo.invalid'").fetchone()[0]
        camera = conn.execute("SELECT id FROM items WHERE name='Canon EOS R50'").fetchone()[0]
        conn.execute(
            "INSERT INTO bookings(user_id,item_id,booking_date,slot,purpose,status) VALUES(?,?,?,?,?,?)",
            (aarav, camera, tomorrow, "morning", "Film club orientation", "reserved"),
        )


def ensure_database(db_path: Union[str, Path]) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count:
            for old_email, _old_name, new_name, new_email in DEMO_ACCOUNT_MIGRATIONS:
                target = conn.execute(
                    "SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (new_email,)
                ).fetchone()
                if not target:
                    conn.execute(
                        "UPDATE users SET name=?,email=? WHERE email=? COLLATE NOCASE",
                        (new_name, new_email, old_email),
                    )
    if count == 0:
        reset_database(db_path)


def parse_day(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Date is required.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Date must use YYYY-MM-DD.") from exc
    if parsed < date.today():
        raise ValueError("Past dates cannot be booked.")
    if parsed > date.today() + timedelta(days=120):
        raise ValueError("Bookings are limited to 120 days ahead.")
    return parsed.isoformat()


def parse_positive_id(value: object, label: str = "ID") -> int:
    if type(value) is not int or not 1 <= value <= 9_223_372_036_854_775_807:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def parse_path_id(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise ValueError("Booking not found.")
    return parse_positive_id(int(value), "Booking ID")


class BookingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "GLSGearDesk/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def json_response(self, status: int, payload: dict | list, headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, status: int, message: str):
        self.json_response(status, {"error": message})

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid content length.") from exc
        if length <= 0 or length > 16_384:
            raise ValueError("A JSON body is required (maximum 16 KB).")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("Content-Type must be application/json.")
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Malformed JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object.")
        return data

    def current_session(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("campus_session")
        if not morsel:
            return None
        with SESSION_LOCK:
            session = SESSIONS.get(morsel.value)
            if session and session["expires"] >= datetime.now().timestamp():
                return session
            SESSIONS.pop(morsel.value, None)
        return None

    def require_user(self, role=None, csrf=False):
        session = self.current_session()
        if not session:
            self.error_json(HTTPStatus.UNAUTHORIZED, "Sign in to continue.")
            return None
        if role and session["role"] != role:
            self.error_json(HTTPStatus.FORBIDDEN, "This action requires an administrator account.")
            return None
        if csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            self.error_json(HTTPStatus.FORBIDDEN, "Missing or invalid CSRF token.")
            return None
        return session

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response(200, {"status": "ok", "service": "equipment-booking"})
        if parsed.path == "/api/me":
            session = self.require_user()
            if session:
                return self.json_response(200, {"user": session["user"], "csrfToken": session["csrf"]})
            return
        if parsed.path == "/api/items":
            session = self.require_user()
            if not session:
                return
            from urllib.parse import parse_qs
            raw = parse_qs(parsed.query).get("date", [date.today().isoformat()])[0]
            try:
                booking_day = parse_day(raw)
            except ValueError as exc:
                return self.error_json(400, str(exc))
            with connect(self.server.db_path) as conn:
                rows = conn.execute(
                    """SELECT i.*, b.slot AS busy_slot FROM items i
                       LEFT JOIN bookings b ON b.item_id=i.id AND b.booking_date=?
                         AND b.status IN ('reserved','checked_out') ORDER BY i.category,i.id""",
                    (booking_day,),
                ).fetchall()
            items = {}
            for row in rows:
                item = items.setdefault(row["id"], {"id": row["id"], "name": row["name"], "category": row["category"], "description": row["description"], "availability": {slot: True for slot in SLOTS}})
                if row["busy_slot"]:
                    item["availability"][row["busy_slot"]] = False
            return self.json_response(200, {"date": booking_day, "slots": list(SLOTS), "items": list(items.values())})
        if parsed.path == "/api/bookings":
            session = self.require_user()
            if not session:
                return
            with connect(self.server.db_path) as conn:
                rows = conn.execute(
                    """SELECT b.id,b.booking_date,b.slot,b.purpose,b.status,b.created_at,
                              i.name item_name,i.category,u.name user_name,u.email user_email
                       FROM bookings b JOIN items i ON i.id=b.item_id JOIN users u ON u.id=b.user_id
                       WHERE b.user_id=? ORDER BY b.booking_date DESC,b.id DESC""",
                    (session["user"]["id"],),
                ).fetchall()
            return self.json_response(200, {"bookings": [dict(row) for row in rows]})
        if parsed.path == "/api/admin/bookings":
            session = self.require_user("admin")
            if not session:
                return
            with connect(self.server.db_path) as conn:
                rows = conn.execute(
                    """SELECT b.id,b.booking_date,b.slot,b.purpose,b.status,b.created_at,
                              i.name item_name,i.category,u.name user_name,u.email user_email
                       FROM bookings b JOIN items i ON i.id=b.item_id JOIN users u ON u.id=b.user_id
                       ORDER BY CASE b.status WHEN 'checked_out' THEN 0 WHEN 'reserved' THEN 1 ELSE 2 END,
                                b.booking_date,b.id"""
                ).fetchall()
            return self.json_response(200, {"bookings": [dict(row) for row in rows]})
        if parsed.path.startswith("/api/"):
            return self.error_json(404, "API route not found.")
        return self.serve_static(parsed.path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
        except ValueError as exc:
            return self.error_json(400, str(exc))
        if path == "/api/login":
            email = str(data.get("email", "")).strip().lower()
            password = str(data.get("password", ""))
            with connect(self.server.db_path) as conn:
                row = conn.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                return self.error_json(401, "Email or password is incorrect.")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            user = {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}
            with SESSION_LOCK:
                SESSIONS[token] = {"user": user, "role": row["role"], "csrf": csrf, "expires": datetime.now().timestamp() + SESSION_MAX_AGE}
            cookie = f"campus_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
            return self.json_response(200, {"user": user, "csrfToken": csrf}, [("Set-Cookie", cookie)])
        session = self.require_user(csrf=True)
        if not session:
            return
        if path == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", "")); morsel = cookie.get("campus_session")
            if morsel:
                with SESSION_LOCK: SESSIONS.pop(morsel.value, None)
            return self.json_response(200, {"ok": True}, [("Set-Cookie", "campus_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")])
        if path == "/api/bookings":
            try:
                item_id = parse_positive_id(data.get("itemId"), "Item ID"); booking_day = parse_day(data.get("date")); slot = data.get("slot")
                purpose = str(data.get("purpose", "")).strip()
                if slot not in SLOTS: raise ValueError("Slot must be morning or afternoon.")
                if not 3 <= len(purpose) <= 120: raise ValueError("Purpose must be 3 to 120 characters.")
            except (ValueError, TypeError) as exc:
                return self.error_json(400, str(exc) if str(exc) else "Invalid booking input.")
            try:
                with connect(self.server.db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    exists = conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                    if not exists:
                        conn.rollback(); return self.error_json(404, "Equipment item not found.")
                    cur = conn.execute(
                        "INSERT INTO bookings(user_id,item_id,booking_date,slot,purpose,status) VALUES(?,?,?,?,?,'reserved')",
                        (session["user"]["id"], item_id, booking_day, slot, purpose),
                    )
                    conn.commit()
                return self.json_response(201, {"id": cur.lastrowid, "status": "reserved"})
            except sqlite3.IntegrityError:
                return self.error_json(409, "That item was just booked for this slot. Choose another slot.")
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "bookings"] and parts[3] == "cancel":
            try: booking_id = parse_path_id(parts[2])
            except ValueError: return self.error_json(404, "Booking not found.")
            with connect(self.server.db_path) as conn:
                cur = conn.execute(
                    "UPDATE bookings SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status='reserved'",
                    (booking_id, session["user"]["id"]),
                )
            if cur.rowcount == 0: return self.error_json(409, "Only your reserved bookings can be cancelled.")
            return self.json_response(200, {"id": booking_id, "status": "cancelled"})
        if len(parts) == 4 and parts[:2] == ["api", "admin"] and parts[3] in ("checkout", "return"):
            if session["role"] != "admin": return self.error_json(403, "This action requires an administrator account.")
            try: booking_id = parse_path_id(parts[2])
            except ValueError: return self.error_json(404, "Booking not found.")
            action = parts[3]; current, new = ("reserved", "checked_out") if action == "checkout" else ("checked_out", "returned")
            with connect(self.server.db_path) as conn:
                cur = conn.execute("UPDATE bookings SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status=?", (new, booking_id, current))
            if cur.rowcount == 0: return self.error_json(409, f"Booking cannot be marked {new.replace('_',' ')} from its current status.")
            return self.json_response(200, {"id": booking_id, "status": new})
        return self.error_json(404, "API route not found.")

    def serve_static(self, path):
        filename = "index.html" if path in ("", "/") else path.lstrip("/")
        if filename not in {"index.html", "app.js", "styles.css"}:
            return self.send_error(404)
        file = STATIC / filename
        if not file.exists(): return self.send_error(404)
        body = file.read_bytes(); mime = {".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",".css":"text/css; charset=utf-8"}[file.suffix]
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(body))); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Run the GLS Gear Desk equipment booking demo")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--reset-demo", action="store_true", help="replace local demo data (server must be stopped)")
    args = parser.parse_args()
    lock = Path(str(args.db) + ".server.lock")
    if args.reset_demo:
        if lock.exists():
            try:
                pid = int(lock.read_text().strip()); os.kill(pid, 0)
                raise SystemExit(f"Refusing reset: demo server PID {pid} is running. Stop it first.")
            except (ValueError, ProcessLookupError): lock.unlink(missing_ok=True)
        reset_database(args.db); print(f"Reset local synthetic demo database: {args.db}"); return
    ensure_database(args.db)
    if lock.exists():
        try:
            existing_pid = int(lock.read_text().strip())
            os.kill(existing_pid, 0)
            raise SystemExit(f"Server lock belongs to running PID {existing_pid}.")
        except (ValueError, ProcessLookupError):
            lock.unlink(missing_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        raise SystemExit("Server lock exists. Stop the other server or remove a stale .server.lock file.")
    try:
        server = BookingServer(("127.0.0.1", args.port), Handler, args.db)
    except Exception:
        lock.unlink(missing_ok=True)
        raise
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"GLS Gear Desk running at http://localhost:{server.server_port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
