# CARRERA-HUB Stage 4R2

Package-isolated recovery diagnostics and strict failure-state handling.

Built from the Stage 3B startup-full-terminal-reset baseline and the Stage 4R1
experiment. Runtime terminal reset behavior is unchanged: full `reset` occurs
once before the first dashboard frame and never during recovery/runtime.


## Phase D1 - Device Registration & Heartbeat

D1 adds an optional device control-plane agent. It creates a stable random
`device_id` in `device.json`, can register the device with a future CARRERA
gateway, and can send periodic heartbeat snapshots. The feature is disabled
by default and is isolated from the Roblox recovery engine.


### D1 configuration

D1 is disabled by default. To enroll a device later, configure:

- `DEVICE_AGENT_ENABLED=1`
- `DEVICE_NAME="Device-01"`
- `DEVICE_GATEWAY_URL="https://..."`
- `DEVICE_ENROLLMENT_TOKEN="..."`
- `DEVICE_HEARTBEAT_INTERVAL=30`
- `DEVICE_GATEWAY_TIMEOUT=8`

The gateway contract for this stage is:

- `POST /api/v1/devices/register` for initial registration.
- `POST /api/v1/devices/heartbeat` for periodic presence updates.

Registration may return `device_token`; CARRERA-HUB stores it in the local
`device.json` identity file and uses it for subsequent heartbeats.

## Phase D2-L - Local Controller Hub

D2-L remodels the D1 gateway into an optional Controller Hub embedded directly
inside CARRERA-HUB. No separate gateway process is required on the Controller
Device. One CARRERA-HUB instance can remain a normal Roblox worker while also
serving the device registry and status API.

Enable D2-L only on the designated Controller Device:

- `CONTROLLER_ENABLED=1`
- `CONTROLLER_HOST="0.0.0.0"`
- `CONTROLLER_PORT=8765`
- `CONTROLLER_ENROLLMENT_TOKEN="..."`
- `CONTROLLER_STATUS_TOKEN="..."`
- `CONTROLLER_OFFLINE_AFTER=90`

Worker devices continue using the D1 agent, but set:

- `DEVICE_AGENT_ENABLED=1`
- `DEVICE_GATEWAY_URL="http://<controller-address>:8765"`
- `DEVICE_ENROLLMENT_TOKEN="<same-enrollment-token>"`

D2-L does not add Discord commands yet. The read-only status endpoints are the
foundation for the Discord layer planned for D3.
