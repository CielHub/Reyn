"""CARRERA-HUB Phase D2-L: embedded local Controller Hub.

The controller is intentionally part of CARRERA-HUB itself.  It provides a
small authenticated HTTP control-plane endpoint for worker devices, stores a
local device registry, and exposes read-only global status for the future
Discord layer.

It is disabled by default and runs independently from the Roblox recovery
engine. If the controller stops or becomes unreachable, worker CARRERA-HUB
instances continue running normally.
"""

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.device_agent import collect_runtime_snapshot, load_or_create_identity
from core.logger import log

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_FILE = os.path.join(BASE_DIR, "controller_registry.json")
PROTOCOL_VERSION = 2


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch():
    return time.time()


class DeviceRegistry:
    def __init__(self, path=REGISTRY_FILE, offline_after=90):
        self.path = path
        self.offline_after = max(30, int(offline_after))
        self._lock = threading.RLock()
        self.devices = {}
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.devices = data if isinstance(data, dict) else {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self.devices = {}

    def _save_locked(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.devices, fh, indent=2, ensure_ascii=True)
            fh.write("\n")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def register(self, payload):
        device_id = str(payload.get("device_id", "")).strip()
        device_secret = str(payload.get("device_secret", "")).strip()
        if not device_id or not device_secret:
            raise ValueError("missing_device_identity")

        with self._lock:
            existing = self.devices.get(device_id, {})
            device_token = existing.get("device_token") or secrets.token_urlsafe(32)
            now = _now()
            self.devices[device_id] = {
                "device_id": device_id,
                "device_name": str(payload.get("device_name", device_id))[:64],
                "platform": str(payload.get("platform", "unknown"))[:64],
                "protocol_version": payload.get("protocol_version", PROTOCOL_VERSION),
                "registered_at": existing.get("registered_at", now),
                "last_seen": now,
                "last_seen_epoch": _epoch(),
                "status": "ONLINE",
                "uptime_seconds": 0,
                "accounts_total": 0,
                "accounts_online": 0,
                "accounts": [],
                # Kept server-side only. Never returned by status endpoints.
                "device_secret": device_secret,
                "device_token": device_token,
            }
            self._save_locked()
            return device_token

    def heartbeat(self, payload, token):
        device_id = str(payload.get("device_id", "")).strip()
        with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise KeyError("device_not_registered")
            if not secrets.compare_digest(str(token), str(device.get("device_token", ""))):
                raise PermissionError("unauthorized")
            now = _now()
            device.update({
                "device_name": str(payload.get("device_name", device["device_name"]))[:64],
                "last_seen": now,
                "last_seen_epoch": _epoch(),
                "status": "ONLINE",
                "uptime_seconds": int(payload.get("uptime_seconds", 0) or 0),
                "accounts_total": int(payload.get("accounts_total", 0) or 0),
                "accounts_online": int(payload.get("accounts_online", 0) or 0),
                "accounts": payload.get("accounts", []) if isinstance(payload.get("accounts", []), list) else [],
            })
            self._save_locked()

    def _public_device(self, device):
        item = dict(device)
        item.pop("device_secret", None)
        item.pop("device_token", None)
        last_seen_epoch = float(item.get("last_seen_epoch", 0) or 0)
        item["age_seconds"] = max(0, int(_epoch() - last_seen_epoch)) if last_seen_epoch else None
        item["status"] = "ONLINE" if last_seen_epoch and (item["age_seconds"] <= self.offline_after) else "OFFLINE"
        return item

    def public_status(self):
        with self._lock:
            devices = [self._public_device(d) for d in self.devices.values()]
        devices.sort(key=lambda d: (d.get("device_name", "").lower(), d.get("device_id", "")))
        online = sum(1 for d in devices if d["status"] == "ONLINE")
        accounts_total = sum(int(d.get("accounts_total", 0) or 0) for d in devices)
        accounts_online = sum(int(d.get("accounts_online", 0) or 0) for d in devices)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "updated_at": _now(),
            "devices_total": len(devices),
            "devices_online": online,
            "devices_offline": len(devices) - online,
            "accounts_total": accounts_total,
            "accounts_online": accounts_online,
            "devices": devices,
        }

    def public_devices(self):
        return self.public_status()["devices"]


class _Handler(BaseHTTPRequestHandler):
    server_version = "CARRERA-D2L-Controller/1"

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("invalid_json")

    def _bearer(self):
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return value[len(prefix):].strip() if value.startswith(prefix) else ""

    def do_GET(self):
        if self.path == "/api/v1/controller/status":
            if not secrets.compare_digest(self._bearer(), self.server.status_token):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            self._json(200, {"ok": True, **self.server.registry.public_status()})
            return
        if self.path == "/api/v1/controller/devices":
            if not secrets.compare_digest(self._bearer(), self.server.status_token):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            self._json(200, {"ok": True, "devices": self.server.registry.public_devices()})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        try:
            payload = self._read_payload()
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/v1/devices/register":
            if not secrets.compare_digest(self._bearer(), self.server.enrollment_token):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                token = self.server.registry.register(payload)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "device_token": token, "protocol_version": PROTOCOL_VERSION})
            return

        if self.path == "/api/v1/devices/heartbeat":
            token = self._bearer()
            try:
                self.server.registry.heartbeat(payload, token)
            except KeyError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
                return
            except PermissionError:
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            self._json(200, {"ok": True, "server_time": _now(), "protocol_version": PROTOCOL_VERSION})
            return

        self._json(404, {"ok": False, "error": "not_found"})

    def log_message(self, fmt, *args):
        log.info(f"CONTROLLER: {fmt % args}")


class ControllerHub:
    """Optional embedded controller server running beside CARRERA-HUB."""

    def __init__(self, config_data):
        self.config = config_data
        self.enabled = str(config_data.get("CONTROLLER_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
        self.host = str(config_data.get("CONTROLLER_HOST", "0.0.0.0"))
        self.port = max(1024, int(config_data.get("CONTROLLER_PORT", 8765)))
        self.enrollment_token = str(config_data.get("CONTROLLER_ENROLLMENT_TOKEN", "")).strip()
        self.status_token = str(config_data.get("CONTROLLER_STATUS_TOKEN", "")).strip()
        self.offline_after = max(30, int(config_data.get("CONTROLLER_OFFLINE_AFTER", 90)))
        self.registry = DeviceRegistry(offline_after=self.offline_after)
        self.server = None
        self.thread = None
        self.identity = None
        self._self_thread = None
        self._stop_event = threading.Event()
        self.started_at = time.time()

    def start(self):
        if not self.enabled:
            log.info("CONTROLLER: D2-L disabled (CONTROLLER_ENABLED=0).")
            return False
        if not self.enrollment_token or not self.status_token:
            log.warning("CONTROLLER: Enrollment/status token belum dikonfigurasi. Hub tidak dijalankan.")
            return False
        if self.thread and self.thread.is_alive():
            return True

        self.identity = load_or_create_identity(self.config.get("DEVICE_NAME", ""))
        self.server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.server.registry = self.registry
        self.server.enrollment_token = self.enrollment_token
        self.server.status_token = self.status_token
        self.thread = threading.Thread(target=self.server.serve_forever, name="carrera-controller-hub", daemon=True)
        self.thread.start()
        self._self_thread = threading.Thread(target=self._refresh_self, name="carrera-controller-self", daemon=True)
        self._self_thread.start()
        log.info(f"CONTROLLER: D2-L aktif di {self.host}:{self.port}.")
        return True

    def stop(self):
        self._stop_event.set()
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self._self_thread and self._self_thread.is_alive():
            self._self_thread.join(timeout=2)
        self.server = None
        self.thread = None

    def _refresh_self(self):
        # The controller device itself is represented in the registry without
        # needing a loopback HTTP request or a second device-agent thread.
        while not self._stop_event.is_set():
            try:
                snapshot = collect_runtime_snapshot()
                with self.registry._lock:
                    existing = self.registry.devices.get(self.identity["device_id"], {})
                    self.registry.devices[self.identity["device_id"]] = {
                        "device_id": self.identity["device_id"],
                        "device_name": self.identity.get("device_name", self.identity["device_id"]),
                        "platform": "android-termux-controller",
                        "protocol_version": PROTOCOL_VERSION,
                        "registered_at": existing.get("registered_at", _now()),
                        "last_seen": _now(),
                        "last_seen_epoch": _epoch(),
                        "status": "ONLINE",
                        "uptime_seconds": max(0, int(time.time() - self.started_at)),
                        "accounts_total": snapshot["accounts_total"],
                        "accounts_online": snapshot["accounts_online"],
                        "accounts": snapshot["accounts"],
                        "device_secret": existing.get("device_secret", ""),
                        "device_token": existing.get("device_token", ""),
                    }
                    self.registry._save_locked()
            except Exception as exc:
                log.warning(f"CONTROLLER: Self-status update failed: {exc}")
            self._stop_event.wait(30)


_controller_hub = None


def start_controller_hub(config_data):
    global _controller_hub
    if _controller_hub is None:
        _controller_hub = ControllerHub(config_data)
        _controller_hub.start()
    return _controller_hub


def stop_controller_hub():
    global _controller_hub
    if _controller_hub is not None:
        _controller_hub.stop()
        _controller_hub = None
