"""
Modul: deeplink.py
Tanggung Jawab: Mengonversi target Roblox (Private Server / Place ID) menjadi Android Intent Deep Link.
"""
import re
from core.logger import log


def get_place_intent(place_id):
    """Membuat deep link Roblox untuk langsung membuka sebuah Place ID.

    FIX: sebelumnya pakai skema custom `roblox://placeId=<id>` ("Direct to
    app") -- format ini SUDAH DEPRECATED RESMI oleh Roblox (dikonfirmasi di
    create.roblox.com/docs/production/promotion/deeplinks: "This process is
    deprecated. To create public deep links, use share links.") dan makin
    gak reliable di app Roblox versi baru buat sebagian game -- gejalanya
    Roblox kebuka tapi gak pernah masuk ke map (place ID 113290951185459 /
    Anime Dice, dilaporkan staff).

    Sekarang pakai format `https://www.roblox.com/games/start?placeId=<id>`
    ("Web list to app" di dokumentasi yang sama, BUKAN yang deprecated).
    Ini link https biasa yang di-`am start` LANGSUNG ke package Roblox
    (`-p <pkg>`), jadi Android mencocokkan ke intent-filter App Link Roblox
    utk domain roblox.com (yang sama persis dipakai kalau user tap link
    Roblox dari Discord/WhatsApp lalu langsung kebuka di app) -- bukan lewat
    browser sama sekali. Keuntungan lain: bisa dibangun otomatis dari angka
    Place ID doang (staff TIDAK perlu cari Share Link manual lagi).
    """
    value = str(place_id).strip()
    if not re.fullmatch(r"\d+", value) or int(value) <= 0:
        raise ValueError("Place ID harus berupa angka positif.")

    log.info(f"DEEPLINK: Place ID {value} dikonversi ke Intent (format https, bukan skema deprecated).")
    return f"https://www.roblox.com/games/start?placeId={value}"


def get_lobby_intent():
    """Membuat deep link Roblox tanpa Place ID / Private Server (hanya buka ke Menu/Lobby/Home).

    Skema `roblox://` polos (tanpa parameter apa pun) ini BUKAN bagian dari
    "Direct to app" deep link yang dideprecated Roblox (yang dideprecated
    spesifik `roblox://placeId=...`) -- ini cuma trigger buka app-nya saja
    ke halaman Home, jadi tetap dipertahankan apa adanya.
    """
    log.info("DEEPLINK: Mode Lobby Only aktif, membuka Roblox ke Menu/Home tanpa join map.")
    return "roblox://"


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

    # FIX: sama seperti get_place_intent() -- pakai https (bukan skema
    # roblox:// yang dideprecated) supaya konsisten reliable, memanfaatkan
    # parameter `linkCode` yang didukung format https ini juga (lihat
    # tabel parameter deep link resmi Roblox).
    log.info("DEEPLINK: URL Berhasil dikonversi ke Intent (Format Lama, https).")
    return f"https://www.roblox.com/games/start?placeId={place_id}&linkCode={link_code}"

