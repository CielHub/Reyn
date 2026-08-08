# Phase D2 - Discord Global Status

D2 adds a read-only Discord `/status` command. The bot queries the gateway's
aggregated device status endpoint and renders a compact global overview.

## Dependency

Run the bot in a dedicated environment with `discord.py` installed. The main
CARRERA-HUB Android/Termux environment does not need the Discord dependency.

## Configuration

Copy `bot.conf.example` to `bot.conf` and set:

- `DISCORD_BOT_TOKEN`
- `CARRERA_GATEWAY_URL`
- `CARRERA_GATEWAY_STATUS_TOKEN`

Then run:

```bash
python discord_bot/d2_bot.py
```

The bot provides only `/status` in D2. No restart, kill, stop, or recovery
command is exposed yet.
