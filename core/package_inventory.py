"""
Modul: core/package_inventory.py
Tanggung Jawab (STEP B / Priority P2, lihat CARRERA_RECOMMENDATION_NEXT_STEP_
SPEC.txt bag. 5+6+8): scan package Roblox yang TERINSTAL di device, KHUSUS
untuk jalur agent/headless -- terpisah TOTAL dari core/scanner.py.

KENAPA MODUL BARU, BUKAN REUSE core/scanner.py:
core/scanner.py.get_roblox_packages() dipakai HANYA oleh mode manual/menu
(core/menu.py, core/tester.py, core/tools/username_probe.py -- lihat
PROJECT_CONTEXT.txt bag. 11) dan SENGAJA sys.exit(1) kalau tidak menemukan
package sama sekali -- perilaku itu masuk akal untuk menu interaktif (operator
langsung tahu ada masalah), tapi FATAL kalau ikut kepanggil dari jalur
agent/heartbeat background (akan mematikan seluruh proses CARRERA-HUB, bukan
cuma fitur integrasi Joki Bot). PROJECT_CONTEXT.txt bag. 11 sudah eksplisit
memperingatkan ini: "JANGAN dipanggil langsung dari jalur agent/heartbeat
tanpa dibungkus ulang, itu kerjaan STEP B nanti." Modul ini adalah pembungkus
itu, dengan kontrak yang berbeda sama sekali (lihat di bawah).

DESAIN (pemisahan 3 lapisan CARRERA, lihat spec bag. 5+8):
- INVENTORY (modul ini): "apa saja package Roblox yang terinstal di device?"
  -- availability murni, TIDAK tahu apa-apa soal session/order/monitor manual.
- SESSION AGENT (core/session_agent.py, SESSIONS dict): "package mana yang
  sedang dikelola session/order aktif?"
- MANUAL MONITOR (core/monitor.py, lewat _stats_ref di agent_client.py):
  "package mana yang sedang dipantau operator secara manual lewat menu?"
Ketiganya digabung jadi satu payload HEARTBEAT di
agent_client._snapshot_packages() (lihat STEP B di sana) -- modul ini TIDAK
PERNAH menyentuh SESSIONS atau _stats_ref secara langsung, murni penyedia
data availability.

KONTRAK WAJIB (beda dari core/scanner.py):
- TIDAK PERNAH sys.exit() atau raise ke pemanggil. Kalau tidak ketemu
  package (device belum root-access sepenuhnya / memang belum ada Roblox
  terinstal / command 'pm' gagal), return list kosong -- agent TETAP jalan
  normal, heartbeat tetap terkirim (cuma tanpa data package).
- BLOCKING (subprocess) -- pemanggil WAJIB lewat asyncio.to_thread, sama
  seperti pola scan_username_blocking() di core/username_scanner.py, supaya
  tidak memblokir event loop agent (heartbeat/receive command harus tetap
  jalan paralel).
- Cache in-memory, dibaca NON-BLOCKING lewat get_cached_packages() (dipakai
  agent_client._snapshot_packages(), harus cepat karena ikut jalur heartbeat
  tiap HEARTBEAT_INTERVAL_SECONDS -- TIDAK PERNAH subprocess call langsung
  dari situ).
- Scan berkala TIAP INVENTORY_SCAN_INTERVAL_SECONDS (default 60 detik,
  SENGAJA lebih longgar dari heartbeat 15 detik, sama seperti
  USERNAME_SCAN_INTERVAL_SECONDS di session_agent.py -- "jangan scan berat
  setiap heartbeat", lihat spec bag. 6), BUKAN tiap heartbeat tick. Loop
  berkalanya ada di agent_client._package_inventory_loop().
- Error/kosong CUMA dilog SEKALI (bukan tiap 60 detik) sampai kondisinya
  berubah -- hindari spam log seperti prinsip _username_scanner_loop().
"""
import subprocess
import time

from core.logger import log

INVENTORY_SCAN_INTERVAL_SECONDS = 60

# Hasil scan TERAKHIR + kapan discan. "packages" None berarti belum pernah
# discan sama sekali sejak proses ini start (beda makna dari "sudah discan,
# hasilnya memang kosong").
_cache = {"packages": None, "scanned_at": None}

# Supaya kondisi "tidak ada package / scan gagal" cuma dilog SEKALI saat
# terjadi (dan sekali lagi saat pulih), bukan tiap siklus 60 detik.
_empty_or_failed_logged = False
_last_scan_ok = None


def _run_pm_list_packages() -> str:
    """Return the Android package-manager listing without a shell pipeline.

    Direct execution is preferred. On Termux/root setups where the app UID
    cannot query the full package inventory, retry through ``su``. Both paths
    are explicit argv calls, so no broad shell matching is involved.
    """
    commands = [
        ["/system/bin/pm", "list", "packages"],
        ["pm", "list", "packages"],
        ["su", "-c", "/system/bin/pm list packages"],
    ]
    last_error = None
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout or ""
            last_error = RuntimeError(
                f"command exit={completed.returncode}: {command[0]}"
            )
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("pm list packages failed")


def scan_installed_packages_blocking() -> list:
    """Scan installed Roblox packages and update the cache only on success.

    CRITICAL ISOLATION/LIVENESS RULE:
    - A successful ``pm list packages`` with zero Roblox matches is a real
      empty inventory and may replace the cache with ``[]``.
    - A command/process/permission failure is *not* an empty inventory. In
      that case the previous last-known-good cache is preserved so one
      transient Android/Termux failure cannot make Discord hide every package.

    The function never exits or raises to the agent caller.
    """
    global _empty_or_failed_logged, _last_scan_ok

    try:
        raw_output = _run_pm_list_packages()
        packages = sorted({
            line.split(":", 1)[1].strip()
            for line in raw_output.splitlines()
            if ":" in line and "roblox" in line.lower()
        })
    except Exception:
        _last_scan_ok = False
        if not _empty_or_failed_logged:
            log.warning(
                "PACKAGE_INVENTORY: gagal membaca Android package manager; "
                "mempertahankan last-known-good inventory dan akan mencoba lagi.",
                exc_info=True,
            )
        _empty_or_failed_logged = True
        return get_cached_packages()

    _last_scan_ok = True

    # Command succeeded. An empty result is therefore meaningful and should
    # replace the cache. A non-empty result clears the degraded-state latch.
    if packages:
        if _empty_or_failed_logged:
            log.info(
                f"PACKAGE_INVENTORY: package Roblox kembali terdeteksi "
                f"({len(packages)})."
            )
        _empty_or_failed_logged = False
    elif not _empty_or_failed_logged:
        log.warning(
            "PACKAGE_INVENTORY: pm berhasil, tetapi tidak ada package Roblox "
            "terdeteksi saat ini. Akan dicoba lagi tiap siklus."
        )
        _empty_or_failed_logged = True

    _cache["packages"] = packages
    _cache["scanned_at"] = time.time()
    return list(packages)


def get_scan_status() -> dict:
    """Return lightweight diagnostics for the local agent/TUI."""
    return {
        "scanned": _cache["scanned_at"] is not None,
        "package_count": len(_cache["packages"] or []),
        "scanned_at": _cache["scanned_at"],
        "degraded": _empty_or_failed_logged,
        "last_scan_ok": _last_scan_ok,
    }


def get_cached_packages() -> list:
    """Non-blocking -- daftar package hasil scan TERAKHIR. List kosong kalau
    belum pernah discan sama sekali sejak proses start, ATAU memang tidak
    ada package Roblox ditemukan (pakai has_scanned() untuk membedakan)."""
    return list(_cache["packages"] or [])


def has_scanned() -> bool:
    """True kalau sudah pernah minimal 1x scan sejak proses ini start."""
    return _cache["scanned_at"] is not None
