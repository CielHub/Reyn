"""
Modul: agent_client.py
Tanggung Jawab: jadi jembatan CARRERA-HUB <-> Joki Control Bot lewat
WebSocket. PHASE 1: REGISTER + HEARTBEAT. PHASE 4: agent JUGA mendengarkan
command masuk (START_SESSION) secara bersamaan dengan heartbeat -- lewat 2
coroutine paralel dalam 1 koneksi (asyncio.gather). PHASE 6/7: tambah
STOP_SESSION ke _COMMAND_HANDLERS (tanpa ubah struktur di atas, sesuai
rencana). PHASE 8: tambah SYNC_SESSIONS ke _COMMAND_HANDLERS (satu baris,
struktur routing tetap sama persis -- lihat core/session_agent.py untuk
logic reconcile_sync()).

PRIORITAS P1 (lihat CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt bag. 3+4):
Agent lifecycle sekarang RESTARTABLE dengan aman -- start_agent_background()
boleh dipanggil BERKALI-KALI (mis. dari core/menu.py setiap staff simpan
DEVICE_ID/DEVICE_TOKEN baru di Settings). Panggilan berikutnya otomatis
menghentikan thread/koneksi WS lama dulu (stop_agent_background) sebelum
memulai yang baru -- TIDAK PERNAH ada dua koneksi WS untuk device yang
sama berjalan bersamaan. Status live (CONNECTING/ONLINE/AUTH_FAILED/
RECONNECTING/OFFLINE) tersimpan di _agent_state, dibaca lewat
get_agent_status() -- non-blocking, aman dipanggil dari thread menu utama
kapan saja untuk ditampilkan ke staff.

STEP B / PRIORITAS P2 (lihat CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt
bag. 5+6+8): tambah coroutine ketiga _package_inventory_loop() ke
asyncio.gather yang sama dengan _heartbeat_loop/_receive_loop -- scan
berkala package Roblox yang TERINSTAL (core/package_inventory.py, BUKAN
core/scanner.py yang sys.exit kalau kosong) supaya heartbeat SELALU bisa
melaporkan package walau belum ada session/monitor manual yang menyentuhnya
sama sekali. _snapshot_packages() sekarang menggabungkan TIGA lapisan:
inventory (baseline IDLE) -> _stats_ref/monitor manual -> session_agent
(paling prioritas) -- lihat docstring _snapshot_packages() untuk urutannya.

PRINSIP OFFLINE-FIRST (wajib dijaga):
- Modul ini jalan di THREAD/EVENT LOOP TERPISAH dari core/monitor.py.
- Kalau koneksi ke bot putus, modul ini cuma retry-reconnect dengan
  backoff -- TIDAK PERNAH menyentuh/menghentikan proses monitoring atau
  recovery lokal yang sedang berjalan (baik mode manual/menu maupun
  package yang dikelola headless lewat core/session_agent.py).
- Kalau config device (DEVICE_ID/DEVICE_TOKEN) belum diisi, modul ini
  diam saja (tidak jalan) -- tidak memaksa fitur ini aktif untuk user
  yang belum ikut sistem integrasi.
"""
import asyncio
import json
import threading

try:
    import websockets
except ImportError:
    websockets = None

from core.logger import log
from core import session_agent
from core import username_scanner
from core import package_inventory

HEARTBEAT_INTERVAL_SECONDS = 15
RECONNECT_BACKOFF_SECONDS = [2, 5, 10, 20, 30]  # naik bertahap, lalu tetap 30s

# Diisi oleh monitor.py kalau operator juga menjalankan mode manual/menu
# (referensi ke stats dict punya monitor.py). Opsional -- kalau tidak
# dipanggil, heartbeat cuma melaporkan package yang dikelola headless
# lewat session_agent.py (kalau ada).
_stats_ref = {"packages": {}}

# PHASE P1 (lifecycle): lock + referensi thread/loop/task agent yang
# SEDANG jalan (atau None kalau tidak ada). Dipakai stop_agent_background()
# supaya start_agent_background() aman dipanggil ulang.
_lock = threading.Lock()
_agent_loop = None
_agent_thread = None
_agent_main_task = None

# Status live agent, non-blocking-readable lewat get_agent_status().
# status: OFFLINE | CONNECTING | ONLINE | AUTH_FAILED | RECONNECTING
_agent_state = {"status": "OFFLINE", "device_id": "", "reason": ""}


def set_stats_reference(stats: dict) -> None:
    """Dipanggil dari monitor.py/main.py supaya heartbeat bisa melaporkan
    status package mode manual/menu juga. Opsional -- kalau tidak
    dipanggil, heartbeat tetap jalan hanya dengan data dari session_agent.py."""
    _stats_ref["packages"] = stats


def get_agent_status() -> dict:
    """Non-blocking -- snapshot status agent terkini (status/device_id/reason),
    dipakai core/menu.py buat menampilkan status live ke staff (lihat P1 di
    CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt bag. 4). Aman dipanggil dari
    thread mana pun (cuma baca dict, tidak ada I/O)."""
    return dict(_agent_state)


def _set_status(status: str, device_id=None, reason: str = "") -> None:
    _agent_state["status"] = status
    if device_id is not None:
        _agent_state["device_id"] = device_id
    _agent_state["reason"] = reason


def _snapshot_packages() -> dict:
    """STEP B: gabungkan TIGA lapisan info package (lihat
    CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt bag. 5+8) jadi SATU payload
    heartbeat -- read-only, tidak pernah menulis balik ke cache/stats/
    SESSIONS milik modul lain. TIDAK ADA pesan WS baru, ws_server.py sisi
    bot tetap schema-agnostic untuk isi packages (tidak perlu diedit).

    Urutan (belakangan menang -- override, bukan merge per-field):
    1. INVENTORY (package_inventory.py, BARU STEP B) -- baseline: SEMUA
       package Roblox yang terinstal di device dilaporkan IDLE, supaya
       Discord tahu package itu ADA walau belum pernah dipakai session
       ataupun dipantau manual sama sekali (spec bag. 5: "Discord tidak
       perlu menebak apakah package ada"). Kalau inventory belum sempat
       scan sama sekali (has_scanned() False), baseline ini kosong --
       heartbeat tetap terkirim seperti biasa, cuma tanpa data package,
       TIDAK memblokir/menunda heartbeat menunggu scan pertama selesai.
    2. MANUAL MONITOR (_stats_ref, diisi monitor.py kalau operator buka
       menu) -- overwrite baseline utk package yang sedang dipantau
       manual, state live dari state_machine mode manual (perilaku SAMA
       seperti sebelum STEP B).
    3. SESSION AGENT (session_agent.py, SESSIONS dict) -- overwrite lagi
       utk package yang sedang dikelola session/order aktif, PALING
       prioritas (perilaku SAMA seperti sebelum STEP B).

    Package yang TIDAK muncul di inventory tapi KEBETULAN sedang dikelola
    manual/session (mis. inventory belum sempat scan pertama kali, atau
    'pm list packages' sesaat gagal) TETAP dilaporkan apa adanya dari
    layer 2/3 -- inventory di sini cuma baseline/availability, BUKAN
    filter atau whitelist yang bisa menyembunyikan package lain.
    """
    snapshot = {
        pkg: {"pid": "-", "state": "IDLE", "username": username_scanner.get_cached_username(pkg)}
        for pkg in package_inventory.get_cached_packages()
    }
    for pkg, s in _stats_ref["packages"].items():
        snapshot[pkg] = {"pid": s.get("pid", "-"), "state": s.get("status", "UNKNOWN")}
    snapshot.update(session_agent._snapshot_for_heartbeat())
    return snapshot


# PHASE 4+6/7+8: command masuk dari bot -> handler async(msg, device_id) ->
# dict hasil (COMMAND_RESULT/SYNC_RESPONSE) atau None (tidak perlu balas apa-apa).
_COMMAND_HANDLERS = {
    "START_SESSION": session_agent.handle_start_session,
    "STOP_SESSION": session_agent.handle_stop_session,
    "SYNC_SESSIONS": session_agent.handle_sync_sessions,
}


async def _handle_incoming_command(ws, msg: dict, device_id: str) -> None:
    msg_type = msg.get("type")
    handler = _COMMAND_HANDLERS.get(msg_type)
    if not handler:
        log.info(f"AGENT: command '{msg_type}' dari bot belum didukung di phase ini, diabaikan.")
        return
    try:
        result = await handler(msg, device_id)
    except Exception:
        log.error(f"AGENT: exception saat proses command '{msg_type}'.", exc_info=True)
        return
    if result:
        try:
            await ws.send(json.dumps(result))
        except Exception:
            log.warning(f"AGENT: gagal kirim COMMAND_RESULT untuk '{msg_type}' (koneksi mungkin putus).")


async def _run_agent(device_id: str, token: str, ws_url: str) -> None:
    attempt = 0
    while True:
        try:
            _set_status("CONNECTING", device_id, "")
            async with websockets.connect(ws_url, open_timeout=10) as ws:
                await ws.send(json.dumps({
                    "type": "REGISTER",
                    "device_id": device_id,
                    "device_name": device_id,
                    "token": token,
                }))
                reply = json.loads(await ws.recv())
                if reply.get("type") != "REGISTER_OK":
                    reason = reply.get("reason", "")
                    log.error(f"AGENT: REGISTER ditolak bot ({reason}). "
                              f"Cek DEVICE_ID/DEVICE_TOKEN di config.conf.")
                    _set_status("AUTH_FAILED", device_id, reason)
                    # Jangan spam retry cepat kalau memang kredensial salah.
                    await asyncio.sleep(30)
                    continue

                log.info(f"AGENT: Terhubung ke Joki Control Bot sebagai '{device_id}'.")
                _set_status("ONLINE", device_id, "")
                attempt = 0  # reset backoff setelah sukses konek

                # LIFECYCLE REVISION: daftarkan ulang sender SESSION_STATUS
                # session_agent.py setiap kali koneksi (baru/reconnect)
                # terbentuk -- supaya _run_start_flow yang sedang berjalan
                # (atau yang baru dimulai lewat START_SESSION berikutnya)
                # selalu kirim lewat koneksi WS yang AKTIF saat itu, bukan
                # referensi lama yang sudah putus.
                async def _session_status_sender(payload: dict) -> None:
                    await ws.send(json.dumps(payload))

                session_agent.register_sender(_session_status_sender)

                # PHASE 4: heartbeat & terima-command jalan BERSAMAAN dalam 1
                # koneksi. Kalau salah satu gagal (mis. koneksi putus saat
                # kirim heartbeat), gather melempar exception -- ditangkap di
                # except di bawah, lalu reconnect seperti biasa (backoff tetap
                # jalan, monitoring/recovery lokal tidak pernah disentuh).
                await asyncio.gather(
                    _heartbeat_loop(ws, device_id),
                    _receive_loop(ws, device_id),
                    _package_inventory_loop(),  # STEP B (Priority P2)
                )

        except asyncio.CancelledError:
            _set_status("OFFLINE", device_id, "dihentikan")
            raise
        except Exception as e:
            delay = RECONNECT_BACKOFF_SECONDS[min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            log.warning(f"AGENT: Koneksi ke bot terputus/gagal ({e}). "
                        f"Retry dalam {delay}s. Monitoring/recovery lokal TETAP JALAN NORMAL.")
            _set_status("RECONNECTING", device_id, str(e))
            attempt += 1
            await asyncio.sleep(delay)


async def _package_inventory_loop() -> None:
    """STEP B (Priority P2, lihat CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt
    bag. 5+6): scan berkala package Roblox yang TERINSTAL di device lewat
    core/package_inventory.py (BUKAN core/scanner.py -- lihat alasannya di
    docstring modul itu). Berjalan PARALEL dengan _heartbeat_loop dan
    _receive_loop dalam asyncio.gather yang sama di _run_agent() -- kalau
    salah satu gagal (mis. koneksi putus), ketiganya berhenti bersamaan lalu
    _run_agent() reconnect seperti biasa (pola sudah ada sejak PHASE 4).

    Scan PERTAMA langsung dijalankan begitu loop ini mulai (bukan menunggu
    INVENTORY_SCAN_INTERVAL_SECONDS pertama) supaya heartbeat pertama
    setelah REGISTER_OK/reconnect sudah bisa membawa data package, bukan
    kosong. Exception di sini TIDAK PERNAH mematikan loop -- cukup dilog,
    dicoba lagi siklus berikutnya (kontrak yang sama seperti
    scan_installed_packages_blocking() itu sendiri: tidak pernah sys.exit
    atau raise ke pemanggil).
    """
    while True:
        try:
            await asyncio.to_thread(package_inventory.scan_installed_packages_blocking)
        except Exception:
            log.error("AGENT: exception tak terduga saat package inventory scan.", exc_info=True)
        await asyncio.sleep(package_inventory.INVENTORY_SCAN_INTERVAL_SECONDS)


async def _heartbeat_loop(ws, device_id: str) -> None:
    while True:
        await ws.send(json.dumps({
            "type": "HEARTBEAT",
            "device_id": device_id,
            "packages": _snapshot_packages(),
        }))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _receive_loop(ws, device_id: str) -> None:
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("AGENT: pesan dari bot bukan JSON valid, diabaikan.")
            continue
        # Jangan blokir loop terima command lain -- START_SESSION untuk
        # package berbeda harus bisa jalan paralel (1 device bisa 5+ package).
        asyncio.create_task(_handle_incoming_command(ws, msg, device_id))


def stop_agent_background(timeout: float = 5.0) -> None:
    """PHASE P1: hentikan thread+koneksi WS agent yang SEDANG jalan (kalau
    ada), BLOCKING sampai benar-benar berhenti (maks `timeout` detik).
    Dipanggil OTOMATIS dari start_agent_background() sebelum memulai
    koneksi baru -- supaya TIDAK PERNAH ada dua koneksi WS untuk device
    yang sama berjalan bersamaan (lihat CARRERA_RECOMMENDATION_NEXT_STEP_
    SPEC.txt bag. 3: "Jika reconnect otomatis dilakukan, jangan membuat
    dua agent/dua WS connection berjalan bersamaan").
    Aman dipanggil walau tidak ada agent yang jalan (no-op)."""
    global _agent_loop, _agent_thread, _agent_main_task
    with _lock:
        loop, thread, task = _agent_loop, _agent_thread, _agent_main_task
    if not thread or not thread.is_alive():
        return
    if loop is not None and task is not None:
        loop.call_soon_threadsafe(task.cancel)
    thread.join(timeout=timeout)
    if thread.is_alive():
        log.warning("AGENT: thread lama tidak berhenti dalam batas waktu, "
                     "melanjutkan tetap (kemungkinan zombie thread).")
    with _lock:
        _agent_loop = None
        _agent_thread = None
        _agent_main_task = None
    _set_status("OFFLINE", reason="dihentikan")


def start_agent_background(device_id: str, token: str, ws_url: str) -> None:
    """Jalankan agent di thread + event loop terpisah, non-blocking.

    PHASE P1: AMAN dipanggil BERKALI-KALI -- setiap panggilan otomatis
    menghentikan thread/koneksi agent LAMA dulu (stop_agent_background)
    sebelum memulai yang baru. Dipanggil sekali dari main.py saat startup,
    dan dipanggil LAGI dari core/menu.py setiap staff simpan DEVICE_ID/
    DEVICE_TOKEN baru di Settings -- supaya kredensial baru langsung
    dipakai TANPA staff perlu restart aplikasi manual (lihat P1 di
    CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt).

    Kalau websockets tidak terpasang atau konfigurasi kosong, fungsi ini
    cuma mencatat log + update status (lihat get_agent_status()) dan TIDAK
    menjalankan apa pun -- tidak menghentikan startup CARRERA-HUB yang
    sudah ada.
    """
    if websockets is None:
        log.warning("AGENT: library 'websockets' belum terpasang (pip install websockets). "
                    "Fitur integrasi Joki Bot dilewati, CARRERA-HUB tetap jalan normal.")
        _set_status("OFFLINE", device_id or "", "websockets belum terpasang")
        return

    stop_agent_background()  # jaga-jaga -- pastikan tidak ada sisa koneksi lama

    if not device_id or not token or not ws_url:
        log.info("AGENT: DEVICE_ID/DEVICE_TOKEN/BOT_WS_URL belum diisi di config.conf. "
                 "Fitur integrasi Joki Bot dilewati (opsional).")
        _set_status("OFFLINE", device_id or "", "DEVICE_ID/TOKEN belum diisi")
        return

    def _thread_target():
        global _agent_loop, _agent_main_task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with _lock:
            _agent_loop = loop
        try:
            task = loop.create_task(_run_agent(device_id, token, ws_url))
            with _lock:
                _agent_main_task = task
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.error("AGENT: thread agent berhenti karena error tak terduga.", exc_info=True)
        finally:
            loop.close()

    t = threading.Thread(target=_thread_target, name="JokiAgentClient", daemon=True)
    with _lock:
        _agent_thread = t
    _set_status("CONNECTING", device_id, "")
    t.start()
    log.info(f"AGENT: thread agent dimulai, mencoba konek ke {ws_url} ...")


