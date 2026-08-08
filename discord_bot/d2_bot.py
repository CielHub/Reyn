#!/usr/bin/env python3
"""CARRERA-HUB Phase D2: Discord global status command.

This module is an optional control-plane UI. It reads aggregated device state
from the CARRERA gateway and does not execute Roblox/recovery operations.

Dependency: discord.py (install separately in the bot environment).
"""
import json
import os
import urllib.error
import urllib.request

import discord
from discord import app_commands

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.conf")


def load_config(path=CONFIG_FILE):
    data = {}
    if not os.path.isfile(path):
        return data
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def fetch_global_status(base_url, token, timeout=8):
    url = base_url.rstrip("/") + "/api/v1/devices/status"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "CARRERA-HUB-D2/1",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError(f"Gateway HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def format_status(data):
    devices = data.get("devices", [])
    online = int(data.get("online_devices", 0))
    offline = int(data.get("offline_devices", 0))
    total_accounts = int(data.get("total_accounts", 0))
    active_accounts = int(data.get("active_accounts", 0))

    lines = [
        f"🟢 **{online} Devices Online**  |  🔴 **{offline} Offline**",
        f"👤 **Active Accounts: {active_accounts}/{total_accounts}**",
        "",
    ]
    if not devices:
        lines.append("No registered devices.")
    else:
        for device in devices[:20]:
            icon = "🟢" if device.get("status") == "ONLINE" else "🔴"
            name = device.get("device_name") or device.get("device_id", "unknown")
            accounts_online = int(device.get("accounts_online", 0))
            accounts_total = int(device.get("accounts_total", 0))
            lines.append(f"{icon} **{name}**  •  {accounts_online}/{accounts_total} active")
        if len(devices) > 20:
            lines.append(f"… and {len(devices) - 20} more devices")
    return "\n".join(lines)


class CarreraD2Bot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.none()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config = config

    async def setup_hook(self):
        guild_id = self.config.get("DISCORD_GUILD_ID", "").strip()
        if guild_id.isdigit():
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


config = load_config()
TOKEN = config.get("DISCORD_BOT_TOKEN", "")
GATEWAY_URL = config.get("CARRERA_GATEWAY_URL", "")
GATEWAY_TOKEN = config.get("CARRERA_GATEWAY_STATUS_TOKEN", "")

client = CarreraD2Bot(config)


@client.tree.command(name="status", description="Show global CARRERA-HUB device status")
async def status(interaction: discord.Interaction):
    if not GATEWAY_URL or not GATEWAY_TOKEN:
        await interaction.response.send_message(
            "⚠️ CARRERA gateway status belum dikonfigurasi.", ephemeral=True
        )
        return

    await interaction.response.defer()
    try:
        data = fetch_global_status(GATEWAY_URL, GATEWAY_TOKEN)
        embed = discord.Embed(
            title="🌐 CARRERA-HUB GLOBAL STATUS",
            description=format_status(data),
            color=discord.Color.green() if data.get("offline_devices", 0) == 0 else discord.Color.orange(),
        )
        embed.set_footer(text=f"Updated: {data.get('generated_at', 'unknown')}")
        await interaction.followup.send(embed=embed)
    except (OSError, urllib.error.URLError, ValueError, RuntimeError) as exc:
        await interaction.followup.send(f"🔴 Gateway unavailable: `{exc}`")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN belum diatur di discord_bot/bot.conf")
    client.run(TOKEN)
