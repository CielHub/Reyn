# CARRERA-HUB Phase D1 Gateway

Stdlib-only reference gateway used to test device registration and heartbeat before the Discord layer is added.

Start:

```bash
python gateway/d1_gateway.py --host 0.0.0.0 --port 8765 --enrollment-token YOUR_TOKEN
```

Then configure the device:

```text
DEVICE_AGENT_ENABLED=1
DEVICE_NAME="Device-01"
DEVICE_GATEWAY_URL="http://SERVER_IP:8765"
DEVICE_ENROLLMENT_TOKEN="YOUR_TOKEN"
DEVICE_HEARTBEAT_INTERVAL=30
DEVICE_GATEWAY_TIMEOUT=8
```

Endpoints:
- `POST /api/v1/devices/register`
- `POST /api/v1/devices/heartbeat`

This gateway is intentionally minimal. Discord command routing, permissions, queues, and remote actions belong to later phases.
