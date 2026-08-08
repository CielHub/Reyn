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

## Phase D2 - Global Discord Status

D2 adds a read-only `/status` Discord command backed by the gateway. It is
separate from the Android/Termux runtime and does not add remote control yet.
See `phaseD2.md` and `discord_bot/README.md`.
