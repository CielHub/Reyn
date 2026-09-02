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


def scan_installed_packages_blocking() -> list:
    """BLOCKING (subprocess 'pm list packages') -- panggil lewat
    asyncio.to_thread dari caller async (lihat
    agent_client._package_inventory_loop()). TIDAK PERNAH sys.exit() atau
    raise ke pemanggil -- selalu return list (boleh kosong). Update cache
    in-memory SEBELUM return, supaya get_cached_packages() langsung
    konsisten dengan hasil scan ini.
    """
    global _empty_or_failed_logged
    try:
        raw_output = subprocess.check_output(
            ['pm', 'list', 'packages'], text=True, timeout=10,
        )
        packages = sorted(
            line.split(':', 1)[1].strip()
            for line in raw_output.splitlines()
            if 'roblox' in line.lower() and ':' in line
        )
    except Exception:
        log.warning(
            "PACKAGE_INVENTORY: gagal menjalankan 'pm list packages' (root "
            "belum siap / device belum jalan sepenuhnya?). Agent tetap "
            "jalan, akan dicoba lagi siklus berikutnya.",
            exc_info=True,
        )
        packages = []

    if packages:
        if _empty_or_failed_logged:
            log.info(f"PACKAGE_INVENTORY: package Roblox kembali terdeteksi ({len(packages)}).")
        _empty_or_failed_logged = False
    elif not _empty_or_failed_logged:
        log.warning(
            "PACKAGE_INVENTORY: tidak ada package Roblox terdeteksi saat ini "
            "(akan dicoba lagi tiap siklus, TIDAK menghentikan agent -- "
            "beda dari core/scanner.py mode manual yang sys.exit)."
        )
        _empty_or_failed_logged = True

    _cache["packages"] = packages
    _cache["scanned_at"] = time.time()
    return packages


def get_cached_packages() -> list:
    """Non-blocking -- daftar package hasil scan TERAKHIR. List kosong kalau
    belum pernah discan sama sekali sejak proses start, ATAU memang tidak
    ada package Roblox ditemukan (pakai has_scanned() untuk membedakan)."""
    return list(_cache["packages"] or [])


def has_scanned() -> bool:
    """True kalau sudah pernah minimal 1x scan sejak proses ini start."""
    return _cache["scanned_at"] is not None
