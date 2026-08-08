# CARRERA-HUB Phase D2 - Global Discord Status

D2 adds a read-only global monitoring layer on top of D1.

## Scope
- Gateway: `GET /api/v1/devices/status`
- Discord bot: `/status`
- Aggregates device online/offline state and account active/total counts.
- Device is considered offline when its last heartbeat exceeds the configured
  gateway timeout (default 90 seconds, minimum 30 seconds).
- Status responses never expose `device_secret` or `device_token`.
- D2 does not execute restart, kill, stop, or recovery commands.

## Separation
The Discord bot is a separate process/environment. The Android/Termux
CARRERA-HUB installation does not require `discord.py`.

## Security
Use a dedicated read-only status token for the D2 endpoint. Do not reuse
Discord bot tokens or device enrollment tokens.

## Status
Experimental. D2 is not a baseline until device/gateway/Discord testing
passes.
