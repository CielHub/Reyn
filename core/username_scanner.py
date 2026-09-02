"""
Modul: core/username_scanner.py
Tanggung Jawab (PHASE 4.5, lihat D2 di PROJECT_CONTEXT.txt): baca username
akun Roblox yang SEDANG login di suatu package, dari file internal Roblox
di device (root only). CUMA membaca -- tidak pernah menulis apa pun ke file
Roblox atau ke package lain.

SUMBER DATA (hasil probe MANUAL di device asli, lihat core/tools/
username_probe.py & PROJECT_CONTEXT.txt bag. 4 D2 -- BUKAN tebakan):
setiap package Roblox punya file SharedPreferences bernama SELALU
'prefs.xml' (BUKAN '<package_name>_preferences.xml' -- itu file lain,
isinya cuma metadata App Cloner, tidak relevan) di:
  /data/data/<package_name>/shared_prefs/prefs.xml
berisi entri standar Android SharedPreferences, contoh yang ditemukan:
  <string name="username">Reyzika400</string>
  <string name="displayName">Reyzika400</string>
Sudah diverifikasi konsisten di 3 package berbeda dengan 3 akun berbeda
(nilai berubah sesuai akun yang login, formatnya sama).

CATATAN PENTING:
- 'userid_long' di file yang sama SELALU KOSONG di device yang sudah
  diperiksa -- JANGAN diandalkan sebagai sumber user ID numerik. Modul ini
  cuma baca 'username' (string), bukan user ID.
- File ini permission-nya rw-rw---- (BUKAN world-readable seperti
  kebanyakan file lain di folder yang sama) -- WAJIB dibaca lewat
  'su -c cat', pola yang sama dengan process_manager.py/join_verifier.py
  di seluruh project ini.
- Modul ini SENGAJA TIDAK membaca folder databases/ (SQLite) -- key
  'username' di prefs.xml sudah terbukti reliable dari hasil probe manual.
  Kalau di masa depan ternyata prefs.xml tidak reliable di versi Roblox
  lain, fallback SQLite BELUM diimplementasikan (perlu probe ulang, bukan
  tebakan lagi).
- scan_username_blocking() BLOCKING (subprocess) -- pemanggil WAJIB lewat
  asyncio.to_thread, JANGAN pernah dipanggil langsung dari event loop
  (lihat _username_scanner_loop() di session_agent.py).
"""
import re
import subprocess
import time

from core.logger import log

_USERNAME_RE = re.compile(r'name="username"[^>]*(?:value="([^"]*)"|>([^<]*)<)')

# Cache in-memory: package_name -> {"username": <str atau None>, "scanned_at": <epoch float>}
# Dibaca CEPAT & NON-BLOCKING lewat get_cached_username() (dipanggil dari
# session_agent._snapshot_for_heartbeat(), harus cepat karena ikut jalur
# heartbeat setiap HEARTBEAT_INTERVAL_SECONDS). Ditulis oleh
# scan_username_blocking() yang dipanggil berkala dari
# session_agent._username_scanner_loop() lewat asyncio.to_thread.
_cache: dict = {}


def scan_username_blocking(pkg: str) -> str:
    """BLOCKING (subprocess 'su -c cat') -- panggil lewat asyncio.to_thread
    dari caller async. Baca username akun Roblox yang lagi login di package
    ini, update cache in-memory, dan return hasilnya. Return None kalau
    tidak ketemu / file tidak bisa dibaca (mis. package belum pernah login
    sama sekali, atau device belum root-access saat itu)."""
    path = f"/data/data/{pkg}/shared_prefs/prefs.xml"
    try:
        result = subprocess.run(
            ["su", "-c", f"cat '{path}'"],
            capture_output=True, text=True, timeout=5, errors="replace",
        )
        content = (result.stdout or "").strip()
    except Exception:
        log.warning(f"USERNAME_SCANNER: exception baca prefs.xml untuk {pkg}.", exc_info=True)
        content = ""

    username = None
    if content:
        m = _USERNAME_RE.search(content)
        if m:
            value = m.group(1) if m.group(1) is not None else (m.group(2) or "")
            value = value.strip()
            username = value or None

    _cache[pkg] = {"username": username, "scanned_at": time.time()}
    return username


def get_cached_username(pkg: str):
    """Non-blocking -- baca hasil scan TERAKHIR dari cache (dipakai
    heartbeat, TIDAK PERNAH melakukan subprocess call sendiri). Return None
    kalau package belum pernah discan sama sekali (mis. baru START_SESSION,
    scan pertama belum jalan)."""
    entry = _cache.get(pkg)
    return entry["username"] if entry else None


def forget(pkg: str) -> None:
    """Hapus cache untuk package ini. Dipanggil session_agent.py begitu
    scanner loop package tsb berhenti (session selesai/STOP_SESSION),
    supaya heartbeat berikutnya tidak melaporkan username basi untuk
    package yang sudah tidak dikelola agent."""
    _cache.pop(pkg, None)
