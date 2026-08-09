# CARRERA-HUB D3R2 - Interactive Discord Monitoring Panel

## Scope
D3R2 builds on D3R1 and adds a read-only interactive Discord panel on the Controller Device.

Commands:
- `/status` - global status embed
- `/panel` - interactive monitoring panel

Panel actions:
- 📊 Status: refresh the global status in the panel
- 📱 Devices: open an ephemeral device picker and inspect a selected device
- ⚠️ Issues: list offline/problematic devices/accounts
- 🔄 Refresh: refresh the main panel

## Safety boundary
D3R2 exposes no restart, stop, kill, recovery, or configuration command. It is read-only.
The Roblox/recovery engine remains independent from Discord runtime failures.

## Dependency
`pip install -U "discord.py>=2.6,<3"`

## Test
1. Configure Discord in the CARRERA-HUB Discord Controller menu.
2. Enable Controller Hub and Discord Bot.
3. Start `python main.py` on the Controller Device.
4. In Discord, run `/panel`.
5. Test Status, Devices, Issues, and Refresh.
6. Select a device from Devices and verify account/PID/state information.

D3R2 is experimental and is not a baseline until explicitly promoted.
