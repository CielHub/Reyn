#!/usr/bin/env python3
"""Minimal CARRERA-HUB Phase D1 gateway.

Stdlib-only reference gateway for registration and heartbeat testing.
This is a control-plane foundation, not the Discord bot yet.
"""
import argparse
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")
LOCK = threading.Lock()
DEVICES = {}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load():
    global DEVICES
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            DEVICES = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        DEVICES = {}


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DEVICES, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(tmp, DATA_FILE)


def auth(header, expected):
    return header == f"Bearer {expected}"


class Handler(BaseHTTPRequestHandler):
    server_version = "CARRERA-D1-Gateway/1"

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid_json"})
            return

        if self.path == "/api/v1/devices/register":
            self.register(payload)
        elif self.path == "/api/v1/devices/heartbeat":
            self.heartbeat(payload)
        else:
            self._json(404, {"ok": False, "error": "not_found"})

    def register(self, p):
        enrollment = self.server.enrollment_token
        if not auth(self.headers.get("Authorization", ""), enrollment):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        device_id = str(p.get("device_id", "")).strip()
        secret = str(p.get("device_secret", "")).strip()
        if not device_id or not secret:
            self._json(400, {"ok": False, "error": "missing_device_identity"})
            return

        with LOCK:
            existing = DEVICES.get(device_id, {})
            device_token = existing.get("device_token") or secrets.token_urlsafe(32)
            DEVICES[device_id] = {
                "device_id": device_id,
                "device_name": p.get("device_name", device_id),
                "platform": p.get("platform", "unknown"),
                "registered_at": existing.get("registered_at", now()),
                "last_seen": now(),
                "status": "ONLINE",
                "accounts_total": 0,
                "accounts_online": 0,
                "accounts": [],
                "device_secret": secret,
                "device_token": device_token,
            }
            save()
        self._json(200, {"ok": True, "device_token": device_token})

    def heartbeat(self, p):
        device_id = str(p.get("device_id", "")).strip()
        with LOCK:
            device = DEVICES.get(device_id)
            if not device:
                self._json(404, {"ok": False, "error": "device_not_registered"})
                return
            if not auth(self.headers.get("Authorization", ""), device.get("device_token", "")):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            device.update({
                "device_name": p.get("device_name", device["device_name"]),
                "last_seen": p.get("timestamp", now()),
                "status": "ONLINE",
                "uptime_seconds": p.get("uptime_seconds", 0),
                "accounts_total": p.get("accounts_total", 0),
                "accounts_online": p.get("accounts_online", 0),
                "accounts": p.get("accounts", []),
            })
            save()
        self._json(200, {"ok": True, "server_time": now()})

    def log_message(self, fmt, *args):
        print(f"[{now()}] {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="CARRERA-HUB Phase D1 gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--enrollment-token", required=True)
    args = parser.parse_args()
    load()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.enrollment_token = args.enrollment_token
    print(f"CARRERA D1 gateway listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
