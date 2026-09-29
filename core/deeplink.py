"""
Modul: deeplink.py
Tanggung Jawab: Mengonversi target Roblox (Private Server / Place ID)
menjadi Android Intent Deep Link.
"""
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import re

from core.logger import log

_ROBLOX_HOST = "roblox.com"


def _is_roblox_host(hostname: str | None) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    return host == _ROBLOX_HOST or host.endswith("." + _ROBLOX_HOST)


def _parse_https_roblox_url(value: str):
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ValueError("URL Roblox tidak valid.") from exc

    if parsed.scheme.lower() != "https" or not _is_roblox_host(parsed.hostname):
        raise ValueError("URL harus merupakan HTTPS URL Roblox yang valid.")

    return parsed


def get_place_intent(place_id):
    """Membuat deep link Roblox untuk langsung membuka sebuah Place ID."""
    value = str(place_id).strip()
    if not re.fullmatch(r"\d+", value) or int(value) <= 0:
        raise ValueError("Place ID harus berupa angka positif.")

    log.info(f"DEEPLINK: Place ID {value} dikonversi ke Intent HTTPS.")
    query = urlencode({"placeId": value})
    return f"https://www.roblox.com/games/start?{query}"


def get_lobby_intent():
    """Membuat intent untuk membuka Roblox ke Home/Lobby."""
    log.info("DEEPLINK: Mode Lobby Only aktif, membuka Roblox ke Menu/Home tanpa join map.")
    return "roblox://"


def _normalize_share_link(value: str) -> str:
    parsed = _parse_https_roblox_url(value)
    path = parsed.path.rstrip("/").lower()
    if not path.startswith("/share"):
        raise ValueError("Share Link Roblox tidak valid.")

    params = parse_qs(parsed.query, keep_blank_values=False)
    code = params.get("code", [""])[0].strip()
    link_type = params.get("type", [""])[0].strip()
    if not code or not link_type:
        raise ValueError("Share Link Roblox harus memiliki code dan type.")

    clean_query = urlencode({"code": code, "type": link_type})
    clean = parsed._replace(query=clean_query, fragment="")
    return urlunparse(clean)


def _parse_legacy_private_server(value: str) -> tuple[str, str]:
    parsed = _parse_https_roblox_url(value)
    place_match = re.search(r"/games/(\d+)(?:/|$)", parsed.path, re.IGNORECASE)
    if not place_match:
        raise ValueError("URL Private Server tidak memiliki Place ID yang valid.")

    params = parse_qs(parsed.query, keep_blank_values=False)
    link_code = ""
    for key in ("privateServerLinkCode", "linkCode"):
        values = params.get(key)
        if values and values[0].strip():
            link_code = values[0].strip()
            break

    if not link_code:
        raise ValueError("URL Private Server tidak memiliki privateServerLinkCode yang valid.")

    return place_match.group(1), link_code


def get_intent_url(private_server_link):
    """Validasi target Roblox dan mengubahnya menjadi URL intent yang aman."""
    value = str(private_server_link or "").strip()
    if not value:
        raise ValueError("Target Roblox kosong.")

    # Place ID langsung.
    if re.fullmatch(r"\d+", value):
        return get_place_intent(value)

    # Share Link baru. Validasi hostname + parameter, bukan sekadar substring '/share'.
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ValueError("Target Roblox tidak valid.") from exc

    if parsed.scheme.lower() == "https" and _is_roblox_host(parsed.hostname):
        if parsed.path.rstrip("/").lower().startswith("/share"):
            log.info("DEEPLINK: Terdeteksi format Share Link Roblox yang valid.")
            return _normalize_share_link(value)

        # Format lama Private Server.
        place_id, link_code = _parse_legacy_private_server(value)
        query = urlencode({"placeId": place_id, "linkCode": link_code})
        log.info("DEEPLINK: URL Private Server lama dikonversi ke Intent HTTPS.")
        return f"https://www.roblox.com/games/start?{query}"

    raise ValueError("Link Private Server / URL Roblox tidak valid.")
