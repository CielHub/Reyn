"""
CARRERA-HUB Phase D1: Device Registration & Heartbeat Agent.

The agent is intentionally independent from the Roblox recovery engine.
It provides a small, optional control-plane client that can register the
current Android/Termux device and periodically publish a heartbeat.

Network communication is disabled unless DEVICE_AGENT_ENABLED=1 and a
DEVICE_GATEWAY_URL is configured. If the gateway is unavailable, the agent
backs off and CARRERA-HUB continues running normally.
"""

import json
import os
import platform
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib import request

from core.logger import log
from core.process_manager import get_pid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDENTITY_FILE = os.path.join(BASE_DIR, "device.json")
PROTOCOL_VERSION = 1


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_identity():
    if not os.path.isfile(IDENTITY_FILE):
        return None
    try:
        with open(IDENTITY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("device_id"):
            return data
    except Exception as exc:
        log.warning(f"DEVICE: Gagal membaca identity file: {exc}")
    return None


def _write_identity(data):
    tmp = IDENTITY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=True)
        fh.write("\n")
    os.replace(tmp, IDENTITY_FILE)
    try:
        os.chmod(IDENTITY_FILE, 0o600)
    except OSError:
        pass


def load_or_create_identity(device_name=""):
    """Return a stable, locally generated device identity.

    The ID is random and persisted locally. It is not derived from IMEI,
    Android serial number, MAC address, or another hardware identifier.
    """
    identity = _read_identity()
    if identity is None:
        identity = {
            "device_id": f"device-{uuid.uuid4().hex[:12]}",
            "device_secret": uuid.uuid4().hex + uuid.uuid4().hex,
            "created_at": _utc_now(),
        }
        _write_identity(identity)
        log.info(f"DEVICE: Identity baru dibuat: {identity['device_id']}")

    if device_name:
        identity["device_name"] = str(device_name).strip()[:64]
        _write_identity(identity)
    elif not identity.get("device_name"):
        identity["device_name"] = identity["device_id"]
        _write_identity(identity)

    return identity


def _gateway_url(base_url, path):
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base + "/" + path.lstrip("/")


def _post_json(url, payload, token="", timeout=8):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CARRERA-HUB-D1/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        if not (200 <= response.status < 300):
            raise RuntimeError(f"HTTP {response.status}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw[:500]}


def _scan_roblox_packages_safely():
    """Scan package names without using scanner.get_roblox_packages().

    The normal scanner is intentionally fatal when no Roblox package exists.
    A heartbeat must never be fatal, so D1 uses a non-throwing local probe.
    """
    try:
        raw = subprocess.check_output(
            ["pm", "list", "packages"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []

    return [
        line.split(":", 1)[1].strip()
        for line in raw.splitlines()
        if ":" in line and "roblox" in line.lower()
    ]


def collect_runtime_snapshot():
    """Collect lightweight device/account information for a heartbeat."""
    packages = _scan_roblox_packages_safely()
    accounts = []
    online_count = 0

    for package in packages:
        try:
            pid = get_pid(package)
        except Exception:
            pid = ""
        online = bool(pid)
        if online:
            online_count += 1
        accounts.append({
            "package": package,
            "pid": pid or None,
            "online": online,
        })

    return {
        "accounts_total": len(accounts),
        "accounts_online": online_count,
        "accounts": accounts,
    }


def build_registration_payload(identity, config_data):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "device_id": identity["device_id"],
        "device_name": identity.get("device_name", identity["device_id"]),
        "device_secret": identity["device_secret"],
        "enrollment_token": config_data.get("DEVICE_ENROLLMENT_TOKEN", ""),
        "platform": "android-termux",
        "python_version": platform.python_version(),
        "carrera_version": "1.0.0",
        "registered_at": _utc_now(),
    }


def build_heartbeat_payload(identity, config_data, started_at=None):
    snapshot = collect_runtime_snapshot()
    uptime = int(time.time() - started_at) if started_at else 0
    return {
        "protocol_version": PROTOCOL_VERSION,
        "device_id": identity["device_id"],
        "device_name": identity.get("device_name", identity["device_id"]),
        "timestamp": _utc_now(),
        "uptime_seconds": max(0, uptime),
        "status": "ONLINE",
        "accounts_total": snapshot["accounts_total"],
        "accounts_online": snapshot["accounts_online"],
        "accounts": snapshot["accounts"],
    }


class DeviceAgent:
    """Optional background registration/heartbeat worker."""

    def __init__(self, config_data):
        self.config = config_data
        self.enabled = str(config_data.get("DEVICE_AGENT_ENABLED", "0")).lower() in {
            "1", "true", "yes", "on"
        }
        self.gateway_url = str(config_data.get("DEVICE_GATEWAY_URL", "")).strip()
        self.enrollment_token = str(config_data.get("DEVICE_ENROLLMENT_TOKEN", "")).strip()
        self.heartbeat_interval = max(10, int(config_data.get("DEVICE_HEARTBEAT_INTERVAL", 30)))
        self.timeout = max(3, int(config_data.get("DEVICE_GATEWAY_TIMEOUT", 8)))
        self.identity = None
        self.started_at = time.time()
        self._stop_event = threading.Event()
        self._thread = None
        self._registered = False
        self._last_error_log = 0.0

    @property
    def device_id(self):
        return self.identity["device_id"]

    @property
    def device_name(self):
        return self.identity.get("device_name", self.device_id)

    def start(self):
        if not self.enabled:
            log.info("DEVICE: Agent D1 nonaktif (DEVICE_AGENT_ENABLED=0).")
            return False
        if not self.gateway_url:
            log.warning("DEVICE: Agent D1 aktif tetapi DEVICE_GATEWAY_URL kosong. Agent tidak dijalankan.")
            return False
        if not self.enrollment_token:
            log.warning("DEVICE: DEVICE_ENROLLMENT_TOKEN kosong. Registrasi gateway belum aman untuk dijalankan.")
            return False

        if self._thread and self._thread.is_alive():
            return True

        self.identity = load_or_create_identity(self.config.get("DEVICE_NAME", ""))

        self._thread = threading.Thread(
            target=self._run,
            name="carrera-device-agent",
            daemon=True,
        )
        self._thread.start()
        log.info(f"DEVICE: Agent D1 aktif untuk {self.device_name} ({self.device_id}).")
        return True

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _run(self):
        first_cycle = True
        while not self._stop_event.is_set():
            try:
                if not self._registered:
                    self._register()
                if self._registered:
                    self._heartbeat()
                else:
                    self._rate_limited_warning("DEVICE: Registrasi belum berhasil; heartbeat ditunda.")
            except Exception as exc:
                self._rate_limited_warning(f"DEVICE: Gateway error: {exc}")
                self._registered = False

            wait = 2 if first_cycle and not self._registered else self.heartbeat_interval
            first_cycle = False
            self._stop_event.wait(wait)

    def _register(self):
        url = _gateway_url(self.gateway_url, "/api/v1/devices/register")
        payload = build_registration_payload(self.identity, self.config)
        response = _post_json(url, payload, token=self.enrollment_token, timeout=self.timeout)

        # Gateway may return a per-device token. Persist it locally only if
        # explicitly supplied; this avoids replacing an existing enrollment
        # token by accident.
        assigned_token = response.get("device_token") if isinstance(response, dict) else None
        if assigned_token:
            self.identity["device_token"] = str(assigned_token)
            _write_identity(self.identity)

        self._registered = True
        log.info(f"DEVICE: Registrasi berhasil untuk {self.device_name}.")

    def _heartbeat(self):
        url = _gateway_url(self.gateway_url, "/api/v1/devices/heartbeat")
        token = self.identity.get("device_token") or self.enrollment_token
        payload = build_heartbeat_payload(self.identity, self.config, self.started_at)
        _post_json(url, payload, token=token, timeout=self.timeout)

    def _rate_limited_warning(self, message, interval=60):
        now = time.time()
        if now - self._last_error_log >= interval:
            log.warning(message)
            self._last_error_log = now


_device_agent = None


def start_device_agent(config_data):
    global _device_agent
    if _device_agent is None:
        _device_agent = DeviceAgent(config_data)
        _device_agent.start()
    return _device_agent


def stop_device_agent():
    global _device_agent
    if _device_agent is not None:
        _device_agent.stop()
        _device_agent = None
