"""CARRERA-HUB D3R2: integrated Discord monitoring panel.

D3R2 is read-only.  It adds an interactive Discord control panel on top of
D3R1's local Controller Hub.  No Roblox/recovery command is exposed yet.
"""
import asyncio
import json
import os
import threading
from urllib import request

try:
    import discord
    from discord.ext import commands
except ImportError:
    discord = None
    commands = None

from core.logger import log


def _enabled(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def mask_secret(value, visible=4):
    value = str(value or "")
    if not value:
        return "<Not Set>"
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * max(4, len(value) - visible) + value[-visible:]


def validate_guild_id(value):
    value = str(value or "").strip()
    return value.isdigit() and len(value) >= 15


def test_discord_token(token, timeout=8):
    token = str(token or "").strip()
    if not token:
        return False, "Bot token kosong."
    req = request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bot {token}",
            "User-Agent": "CARRERA-HUB-D3R2/1",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=max(3, int(timeout))) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            if response.status < 200 or response.status >= 300:
                return False, f"Discord HTTP {response.status}."
            username = str(data.get("username") or "Unknown")
            return True, f"Token valid. Bot: {username}"
    except Exception as exc:
        text = str(exc)
        if "401" in text or "Unauthorized" in text:
            return False, "Bot token ditolak Discord."
        return False, f"Gagal terhubung ke Discord: {text[:120]}"


def _fetch_status(base_url, token, timeout=8):
    url = str(base_url).rstrip("/") + "/api/v1/controller/status"
    req = request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "CARRERA-HUB-D3R2/1",
        },
        method="GET",
    )
    with request.urlopen(req, timeout=max(3, int(timeout))) as response:
        raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
        if response.status < 200 or response.status >= 300 or not data.get("ok"):
            raise RuntimeError(str(data.get("error", f"HTTP {response.status}")))
        return data


def test_controller_connection(config_data):
    url = str(config_data.get("DISCORD_CONTROLLER_URL", "http://127.0.0.1:8765")).strip()
    token = str(config_data.get("DISCORD_CONTROLLER_STATUS_TOKEN", "")).strip()
    if not token:
        token = str(config_data.get("CONTROLLER_STATUS_TOKEN", "")).strip()
    if not token:
        return False, "Controller status token belum dikonfigurasi."
    try:
        data = _fetch_status(url, token, config_data.get("DISCORD_STATUS_TIMEOUT", 8))
        return True, f"Controller online. {data.get('devices_online', 0)}/{data.get('devices_total', 0)} device online."
    except Exception as exc:
        return False, f"Controller tidak dapat dihubungi: {str(exc)[:120]}"


def _status_embed(data, title="🌐 CARRERA-HUB GLOBAL STATUS"):
    if discord is None:
        return None
    online = int(data.get("devices_online", 0) or 0)
    offline = int(data.get("devices_offline", 0) or 0)
    accounts_online = int(data.get("accounts_online", 0) or 0)
    accounts_total = int(data.get("accounts_total", 0) or 0)
    embed = discord.Embed(
        title=title,
        description=(
            f"🟢 **{online}** online  •  🔴 **{offline}** offline\n"
            f"👤 Active Accounts: **{accounts_online}/{accounts_total}**"
        ),
    )
    devices = data.get("devices", []) or []
    if devices:
        lines = []
        for device in devices:
            name = str(device.get("device_name") or device.get("device_id") or "Unknown")
            status = str(device.get("status", "OFFLINE")).upper()
            icon = "🟢" if status == "ONLINE" else "🔴"
            active = int(device.get("accounts_online", 0) or 0)
            total = int(device.get("accounts_total", 0) or 0)
            age = device.get("age_seconds")
            age_text = f"{int(age)}s ago" if age is not None else "never"
            lines.append(f"{icon} **{name}** • {active}/{total} active • {age_text}")
        text = "\n".join(lines)
        if len(text) > 3800:
            text = text[:3770] + "\n…"
        embed.add_field(name="Devices", value=text, inline=False)
    else:
        embed.add_field(name="Devices", value="No devices registered.", inline=False)
    embed.set_footer(text=f"Updated: {data.get('updated_at', 'unknown')}")
    return embed


def _device_embed(device):
    if discord is None:
        return None
    name = str(device.get("device_name") or device.get("device_id") or "Unknown")
    status = str(device.get("status", "OFFLINE")).upper()
    icon = "🟢" if status == "ONLINE" else "🔴"
    active = int(device.get("accounts_online", 0) or 0)
    total = int(device.get("accounts_total", 0) or 0)
    age = device.get("age_seconds")
    age_text = f"{int(age)}s ago" if age is not None else "never"
    embed = discord.Embed(title=f"📱 {name}")
    embed.add_field(name="Status", value=f"{icon} **{status}**", inline=True)
    embed.add_field(name="Accounts", value=f"**{active}/{total}** active", inline=True)
    embed.add_field(name="Heartbeat", value=age_text, inline=True)
    uptime = int(device.get("uptime_seconds", 0) or 0)
    embed.add_field(name="Uptime", value=_format_uptime(uptime), inline=True)
    platform = str(device.get("platform") or "unknown")
    embed.add_field(name="Platform", value=platform, inline=True)
    accounts = device.get("accounts", []) or []
    if accounts:
        lines = []
        for account in accounts[:25]:
            pkg = str(account.get("package") or account.get("pkg") or "unknown")
            state = str(account.get("status") or "UNKNOWN").upper()
            pid = account.get("pid")
            pid_text = str(pid) if pid not in (None, "", "-") else "-"
            icon2 = "🟢" if state in {"FARMING", "ONLINE", "RUNNING"} else ("🟡" if state in {"LOADING", "RECOVERY", "RECOVER"} else "🔴")
            lines.append(f"{icon2} `{pkg}` • `{state}` • PID `{pid_text}`")
        if len(accounts) > 25:
            lines.append(f"… +{len(accounts) - 25} more")
        embed.add_field(name="Accounts", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Accounts", value="No account data.", inline=False)
    return embed


def _format_uptime(seconds):
    seconds = max(0, int(seconds or 0))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


class _PanelView(discord.ui.View if discord else object):
    def __init__(self, controller, timeout=1800):
        if discord:
            super().__init__(timeout=timeout)
        self.controller = controller

    async def _status(self, interaction):
        try:
            data = await asyncio.to_thread(self.controller.fetch_status)
            await interaction.response.edit_message(embed=_status_embed(data), view=_PanelView(self.controller))
        except Exception as exc:
            await interaction.response.send_message(f"❌ Controller status tidak tersedia: `{str(exc)[:180]}`", ephemeral=True)

    if discord:
        @discord.ui.button(label="Status", emoji="📊", style=discord.ButtonStyle.primary, custom_id="carrera:panel:status")
        async def status(self, interaction, button):
            await self._status(interaction)

        @discord.ui.button(label="Devices", emoji="📱", style=discord.ButtonStyle.secondary, custom_id="carrera:panel:devices")
        async def devices(self, interaction, button):
            try:
                data = await asyncio.to_thread(self.controller.fetch_status)
                view = _DeviceSelectView(self.controller, data.get("devices", []) or [])
                await interaction.response.send_message(embed=_status_embed(data, "📱 CARRERA-HUB DEVICES"), view=view, ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"❌ Gagal membaca devices: `{str(exc)[:180]}`", ephemeral=True)

        @discord.ui.button(label="Issues", emoji="⚠️", style=discord.ButtonStyle.secondary, custom_id="carrera:panel:issues")
        async def issues(self, interaction, button):
            try:
                data = await asyncio.to_thread(self.controller.fetch_status)
                devices = data.get("devices", []) or []
                issues = []
                for device in devices:
                    name = str(device.get("device_name") or device.get("device_id") or "Unknown")
                    status = str(device.get("status", "OFFLINE")).upper()
                    if status != "ONLINE":
                        issues.append(f"🔴 **{name}** is **{status}**")
                    for account in device.get("accounts", []) or []:
                        state = str(account.get("status") or "UNKNOWN").upper()
                        if state in {"OFFLINE", "FAILED", "ERROR", "RECOVERY", "RECOVER"}:
                            pkg = str(account.get("package") or account.get("pkg") or "unknown")
                            issues.append(f"🟡 **{name}** • `{pkg}` → **{state}**")
                embed = discord.Embed(title="⚠️ CARRERA-HUB ISSUES")
                embed.description = "\n".join(issues[:30]) if issues else "🟢 No current issues detected."
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"❌ Gagal membaca issues: `{str(exc)[:180]}`", ephemeral=True)

        @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.success, custom_id="carrera:panel:refresh")
        async def refresh(self, interaction, button):
            await self._status(interaction)


class _DeviceSelect(discord.ui.Select if discord else object):
    def __init__(self, controller, devices):
        self.controller = controller
        options = []
        for device in devices[:25]:
            device_id = str(device.get("device_id") or "")
            name = str(device.get("device_name") or device_id or "Unknown")
            status = str(device.get("status", "OFFLINE")).upper()
            active = int(device.get("accounts_online", 0) or 0)
            total = int(device.get("accounts_total", 0) or 0)
            options.append(discord.SelectOption(label=name[:100], value=device_id[:100], description=f"{status} • {active}/{total} active"[:100]))
        if not options:
            options = [discord.SelectOption(label="No devices", value="__none__", description="No registered devices")]
        super().__init__(placeholder="Pilih device...", min_values=1, max_values=1, options=options, custom_id="carrera:devices:select")

    async def callback(self, interaction):
        device_id = self.values[0]
        if device_id == "__none__":
            await interaction.response.send_message("Tidak ada device terdaftar.", ephemeral=True)
            return
        try:
            data = await asyncio.to_thread(self.controller.fetch_status)
            device = next((d for d in data.get("devices", []) if str(d.get("device_id")) == device_id), None)
            if not device:
                await interaction.response.send_message("❌ Device sudah tidak tersedia.", ephemeral=True)
                return
            await interaction.response.send_message(embed=_device_embed(device), ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"❌ Gagal membaca device: `{str(exc)[:180]}`", ephemeral=True)


class _DeviceSelectView(discord.ui.View if discord else object):
    def __init__(self, controller, devices, timeout=300):
        super().__init__(timeout=timeout)
        self.add_item(_DeviceSelect(controller, devices))


class DiscordController:
    """Optional read-only Discord monitoring bot for the Controller Device."""

    def __init__(self, config_data):
        self.config = config_data
        self.enabled = _enabled(config_data.get("DISCORD_BOT_ENABLED", "0"))
        self.token = str(config_data.get("DISCORD_BOT_TOKEN", "")).strip()
        self.controller_url = str(config_data.get("DISCORD_CONTROLLER_URL", "http://127.0.0.1:8765")).strip()
        self.status_token = str(config_data.get("DISCORD_CONTROLLER_STATUS_TOKEN", "")).strip() or str(config_data.get("CONTROLLER_STATUS_TOKEN", "")).strip()
        self.guild_id = str(config_data.get("DISCORD_GUILD_ID", "")).strip()
        self.timeout = max(3, int(config_data.get("DISCORD_STATUS_TIMEOUT", 8)))
        self._thread = None
        self._loop = None
        self._bot = None

    def fetch_status(self):
        return _fetch_status(self.controller_url, self.status_token, self.timeout)

    def start(self):
        if not self.enabled:
            log.info("DISCORD: D3R2 disabled (DISCORD_BOT_ENABLED=0).")
            return False
        if discord is None or commands is None:
            log.warning("DISCORD: discord.py belum terpasang; bot tidak dijalankan.")
            return False
        if not self.token or not self.status_token:
            log.warning("DISCORD: Bot token/controller status token belum lengkap.")
            return False
        if self._thread and self._thread.is_alive():
            return True

        intents = discord.Intents.none()
        bot = commands.Bot(command_prefix="!", intents=intents)
        self._bot = bot
        controller = self

        @bot.event
        async def on_ready():
            try:
                if self.guild_id and self.guild_id.isdigit():
                    guild = discord.Object(id=int(self.guild_id))
                    bot.tree.copy_global_to(guild=guild)
                    await bot.tree.sync(guild=guild)
                else:
                    await bot.tree.sync()
                log.info("DISCORD: Bot online dan /status + /panel siap digunakan.")
            except Exception as exc:
                log.warning(f"DISCORD: Gagal sync slash command: {str(exc)[:120]}")

        @bot.tree.command(name="status", description="Lihat status global CARRERA-HUB.")
        async def status_command(interaction):
            await interaction.response.defer(ephemeral=False)
            try:
                data = await asyncio.to_thread(controller.fetch_status)
                await interaction.followup.send(embed=_status_embed(data))
            except Exception as exc:
                await interaction.followup.send(f"❌ Controller status tidak tersedia: `{str(exc)[:180]}`")

        @bot.tree.command(name="panel", description="Buka panel monitoring CARRERA-HUB.")
        async def panel_command(interaction):
            await interaction.response.defer(ephemeral=False)
            try:
                data = await asyncio.to_thread(controller.fetch_status)
                await interaction.followup.send(embed=_status_embed(data, "🎛️ CARRERA-HUB CONTROL PANEL"), view=_PanelView(controller))
            except Exception as exc:
                await interaction.followup.send(f"❌ Panel tidak dapat memuat status: `{str(exc)[:180]}`")

        def runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(bot.start(self.token))
            except Exception as exc:
                log.warning(f"DISCORD: Bot berhenti: {str(exc)[:160]}")
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=runner, name="carrera-discord", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._loop and self._bot and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._bot.close(), self._loop)
            except Exception:
                pass
        self._thread = None
        self._loop = None
        self._bot = None


_discord_controller = None


def start_discord_controller(config_data):
    global _discord_controller
    _discord_controller = DiscordController(config_data)
    return _discord_controller.start()


def stop_discord_controller():
    global _discord_controller
    if _discord_controller:
        _discord_controller.stop()
    _discord_controller = None
