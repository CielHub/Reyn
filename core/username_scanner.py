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
Kalau di masa depan ternyata prefs.xml tidak reliable di device lain,
fallback SQLite BELUM diimplementasikan (perlu probe ulang, bukan
tebakan lagi).
- scan_username_blocking() BLOCKING (subprocess) -- pemanggil WAJIB lewat
asyncio.to_thread, JANGAN pernah dipanggil langsung dari event loop
(lihat _username_scanner_loop() di session_agent.py).
"""
import re
import subprocess
import time

from core.logger import log

_USERNAME_RE = re.compile(
    r'<(?:string|item|entry)\s+[^>]*name\s*=\s*["\']username["\'][^>]*'
    r'(?:value\s*=\s*["\']([^"\']*)["\']|>\s*([^<]*)\s*<)',
    re.IGNORECASE,
)

# Cache in-memory: package_name -> {"username": <str atau None>, "scanned_at": <epoch float>}
# Dibaca CEPAT & NON-BLOCKING lewat get_cached_username() (dipanggil dari
# session_agent._snapshot_for_heartbeat(), harus cepat karena ikut jalur
# heartbeat setiap HEARTBEAT_INTERVAL_SECONDS). Ditulis oleh
# scan_username_blocking() yang dipanggil berkala dari
# session_agent._username_scanner_loop() lewat asyncio.to_thread.
_cache: dict = {}

# Generasi per package: forget() menaikkan angka ini, sehingga scan yang masih
# jalan di thread (mis. task dicancel di tengah scan) tidak menulis ulang cache
# basi setelah session selesai.
_gen: dict = {}
_last_warn: dict = {}
_WARN_EVERY_SECONDS = 60
_PKG_RE = re.compile(r"^[A-Za-z0-9_.]+$")


def _warn_throttled(pkg: str, msg: str, **kw) -> None:
    """Log warning maksimal 1x/menit per package+pesan (hindari spam tiap scan)."""
    now_ts = time.time()
    key = (pkg, msg)
    if now_ts - _last_warn.get(key, 0) >= _WARN_EVERY_SECONDS:
        _last_warn[key] = now_ts
        log.warning(msg, **kw)


def scan_username_result_blocking(pkg_name):
    """Return a structured result from the same canonical scanner/cache path.

    ``read_ok`` refers to the latest filesystem read, not whether a cached
    username exists. This lets callers distinguish a fresh verified identity
    from a temporary read failure.
    """
    username = scan_username_blocking(pkg_name)
    entry = dict(_cache.get(pkg_name) or {})
    return {
        "username": entry.get("username") if entry.get("read_ok") else None,
        "read_ok": bool(entry.get("read_ok")),
        "scanned_at": float(entry.get("scanned_at") or time.time()),
    }

def get_cached_identity(pkg: str) -> dict:
    """Cheap heartbeat-safe read of the latest username verification state."""
    entry = dict(_cache.get(pkg) or {})
    return {
        "username": entry.get("username"),
        "read_ok": bool(entry.get("read_ok")),
        "scanned_at": float(entry.get("scanned_at") or 0),
    }

def scan_username_blocking(pkg: str):
    """Baca username Roblox yang sedang login dan update cache lokal.

    Kegagalan baca sementara (su/permission/file sedang ditulis/kosong) TIDAK
    menghapus username terakhir yang valid. Hanya file yang terbaca utuh dan
    memang tidak berisi username yang mengosongkan cache (logout beneran).
    """
    if not _PKG_RE.match(pkg or ""):
        _warn_throttled(pkg, f"USERNAME_SCANNER: nama package tidak valid: {pkg!r}, dilewati.")
        return None

    gen = _gen.get(pkg, 0)
    path = f"/data/data/{pkg}/shared_prefs/prefs.xml"
    previous_username = _cache.get(pkg, {}).get("username")

    read_ok = False
    content = ""
    try:
        result = subprocess.run(
            ["su", "-c", f"cat '{path}'"],
            capture_output=True, text=True, timeout=5, errors="replace",
        )
        content = (result.stdout or "").strip()
        read_ok = result.returncode == 0 and bool(content)
        if result.returncode != 0:
            err = (result.stderr or "").strip()[:200]
            _warn_throttled(
                pkg,
                f"USERNAME_SCANNER: gagal baca {path} (rc={result.returncode}): {err or '(stderr kosong)'}",
            )
    except subprocess.TimeoutExpired:
        _warn_throttled(pkg, f"USERNAME_SCANNER: timeout baca prefs.xml untuk {pkg}.")
    except Exception:
        _warn_throttled(pkg, f"USERNAME_SCANNER: exception baca prefs.xml untuk {pkg}.", exc_info=True)

    # File terpotong (sedang ditulis Android) -> anggap gagal baca sementara.
    if read_ok and "</map>" not in content:
        read_ok = False

    if _gen.get(pkg, 0) != gen:
        return None  # forget() dipanggil selama scan; jangan tulis cache basi

    if not read_ok:
        _cache[pkg] = {
            "username": previous_username,
            "scanned_at": time.time(),
            "read_ok": False,
        }
        return previous_username

    username = None
    m = _USERNAME_RE.search(content)
    if m:
        value = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        username = value.strip() or None

    _cache[pkg] = {
        "username": username,
        "scanned_at": time.time(),
        "read_ok": True,
    }
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
    _gen[pkg] = _gen.get(pkg, 0) + 1
    _cache.pop(pkg, None)
