# CARRERA-HUB D3R1 - Integrated Discord Controller Settings

## Scope
D3R1 integrates Discord configuration into the CARRERA-HUB terminal UI.
The Controller Device remains the local D2-L hub. D3R1 is read-only and exposes only `/status`.

## Setup
1. Run `python main.py` on the designated Controller Device.
2. Open **Discord Controller** from the main menu.
3. Enable **Controller Hub** and set its enrollment/status tokens as required by D2-L.
4. Enter the Discord Bot Token. It is masked in the UI.
5. Enter the Discord Server/Guild ID.
6. Use **Test Discord Token**.
7. Use **Test Controller**.
8. Enable the Discord Bot.
9. Restart CARRERA-HUB if the runtime needs a clean start.

Optional dependency for the actual Discord runtime:
`pip install -U "discord.py>=2.6,<3"`

## Security
- Never commit the Discord bot token or controller tokens to Git.
- D3R1 never prints the bot token.
- D3R1 has no restart/stop/kill/recovery Discord commands.
- Discord runtime failure does not stop the CARRERA-HUB recovery engine.

## Local controller
The integrated bot defaults to `http://127.0.0.1:8765` and uses the local Controller Hub status credential.
Workers continue using the D2-L registration/heartbeat path.
