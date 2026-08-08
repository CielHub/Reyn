"""
Modul: deeplink.py
Tanggung Jawab: Mengonversi target Roblox (Private Server / Place ID) menjadi Android Intent Deep Link.
"""
import re
from core.logger import log


def get_place_intent(place_id):
    """Membuat deep link Roblox untuk langsung membuka sebuah Place ID."""
    value = str(place_id).strip()
    if not re.fullmatch(r"\d+", value) or int(value) <= 0:
        raise ValueError("Place ID harus berupa angka positif.")

    log.info(f"DEEPLINK: Place ID {value} dikonversi ke Intent.")
    return f"roblox://placeId={value}"


def get_intent_url(private_server_link):
    """Mengekstrak dan mengonversi URL/Place ID menjadi format yang bisa dieksekusi 'am start'."""
    value = str(private_server_link or "").strip()

    if not value:
        raise ValueError("Target Roblox kosong.")

    # Place ID langsung, misalnya: 123456789
    if re.fullmatch(r"\d+", value):
        return get_place_intent(value)

    # Share Link baru
    if "/share" in value:
        log.info("DEEPLINK: Terdeteksi format Share Link baru.")
        return value

    # Format lama Private Server
    place_id_match = re.search(r'games/(\d+)', value)
    link_code_match = re.search(r'privateServerLinkCode=([^&]+)', value)

    if not place_id_match or not link_code_match:
        raise ValueError("Link Private Server / URL Roblox tidak valid.")

    place_id = place_id_match.group(1)
    link_code = link_code_match.group(1)

    log.info("DEEPLINK: URL Berhasil dikonversi ke Intent (Format Lama).")
    return f"roblox://placeId={place_id}&linkCode={link_code}"
