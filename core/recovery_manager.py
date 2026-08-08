"""
Modul : recovery_manager.py
Tanggung Jawab:
- Mengatur keseluruhan alur recovery Error267 dan Low Memory (GLOBAL MODE).
- Mengatur alur Crash Biasa (SINGLE MODE).
- Mengeksekusi kill pada target dan melakukan peluncuran ulang.
- Pause Watchdog saat Global Recovery aktif.
"""

import threading
import time
from enum import Enum

from core.error_detector import has_event, get_event
from core.memory_guard import has_memory_event, get_memory_event, reset_memory_guard
from core.process_manager import graceful_kill, get_pid
from core.cache_cleaner import clean_package_cache
from core.launcher import launch_and_wait
from core.logger import log

class RecoveryMode(Enum):
    SINGLE = 0
    GLOBAL = 1

class RecoveryManager:

    def __init__(self):
        self._running = False
        self._thread = None
        
        self.packages = []
        self.stats = {}
        self.tracked_pids = {}
        self.intent_url = None
        self.timeout_seconds = 60
        self.config_data = {}
        
        self.recovery_mode = RecoveryMode.SINGLE
        self.global_recovery = False
        
        self.global_status = None
        self.global_countdown = 0
        
        # Lock untuk memastikan anti duplicate recovery
        self.recovery_lock = threading.Lock()

    def configure(self, packages, stats, tracked_pids, intent_url, timeout_seconds, config_data):
        self.packages = packages
        self.stats = stats
        self.tracked_pids = tracked_pids
        self.intent_url = intent_url
        self.timeout_seconds = timeout_seconds
        self.config_data = config_data if config_data else {}

    def start(self):
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="RecoveryManager"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def enter_global_recovery(self):
        self.recovery_mode = RecoveryMode.GLOBAL
        self.global_recovery = True

    def exit_global_recovery(self):
        self.recovery_mode = RecoveryMode.SINGLE
        self.global_recovery = False

    def is_global_recovery(self):
        return self.global_recovery

    def watchdog_paused(self):
        return self.global_recovery

    def kill_all_packages(self):
        for pkg in self.packages:
            pid = self.stats[pkg]["pid"]

            if not pid or pid == "-":
                continue

            graceful_kill(pid, pkg)

            self.stats[pkg]["status"] = "RECOVERY"
            self.stats[pkg]["pid"] = "-"
            self.tracked_pids[pkg] = ""

    def wait_all_dead(self, timeout=15):
        start = time.time()
        while time.time() - start < timeout:
            all_dead = True
            for pkg in self.packages:
                pid = self.stats[pkg]["pid"]
                if pid and pid != "-":
                    all_dead = False
                    break
            
            if all_dead:
                return True
                
            time.sleep(0.2)
            
        return False

    def global_delay(self, delay_seconds):
        self.global_status = "WAITING"
        for sec in range(delay_seconds, 0, -1):
            self.global_countdown = sec
            time.sleep(1)
            
        self.global_status = None
        self.global_countdown = 0

    def get_global_state(self):
        return self.global_status, self.global_countdown

    def _worker(self):
        while self._running:
            
            # ==================================================
            # 1. EVENT: MEMORY GUARD (RAM RENDAH)
            # ==================================================
            while has_memory_event():
                event = get_memory_event()
                
                if event is None:
                    break

                if self.is_global_recovery():
                    continue

                if not self.recovery_lock.acquire(blocking=False):
                    continue

                try:
                    self.enter_global_recovery()
                    
                    avail = event['available_mb']
                    thresh = self.config_data.get("MEMORY_THRESHOLD_MB", 200)
                    is_emerg = event.get('is_emergency', False)
                    
                    log.info(f"[MEMORY] Available RAM : {avail}MB (Threshold : {thresh}MB)")
                    if is_emerg:
                        log.warning("[MEMORY] EMERGENCY LEVEL DETECTED!")
                        
                    log.info(f"[GLOBAL] Killing {len(self.packages)} Packages...")
                    self.kill_all_packages()

                    if not self.wait_all_dead():
                        log.warning("[GLOBAL] Beberapa package lambat di-kill. Melanjutkan eksekusi...")

                    # Delay darurat 5 detik, delay normal sesuai config
                    delay = 5 if is_emerg else self.config_data.get("GLOBAL_RECOVERY_DELAY", 30)
                    
                    log.info(f"[GLOBAL] Waiting {delay} Seconds...")
                    self.global_delay(delay)

                    log.info("[GLOBAL] Launching Packages...")
                    self.launch_all_packages()

                    log.info("[GLOBAL] Recovery Completed. Resuming watchdog.")
                finally:
                    self.exit_global_recovery()
                    self.recovery_lock.release()
                    reset_memory_guard()

            # ==================================================
            # 2. EVENT: ERROR 267 DLL (IN-GAME)
            # ==================================================
            while has_event():
                event = get_event()
                
                if event is None:
                    break
                
                reason = event.get("reason")
                
                if reason in (266, 267, 277, 279, 280):
                    is_enabled = self.config_data.get("GLOBAL_RECOVERY_ENABLED", True)
                    if not is_enabled:
                        continue
                        
                    if self.is_global_recovery():
                        continue
                        
                    if not self.recovery_lock.acquire(blocking=False):
                        continue

                    try:
                        self.enter_global_recovery()
                        
                        log.info(f"[ERROR{reason}] Detected on PID {event.get('pid')}. Initiating Global Recovery.")
                        log.info(f"[GLOBAL] Killing {len(self.packages)} Packages...")
                        self.kill_all_packages()
                        
                        if not self.wait_all_dead():
                            log.warning("[GLOBAL] Beberapa package lambat di-kill. Melanjutkan eksekusi...")
                            
                        delay = self.config_data.get("GLOBAL_RECOVERY_DELAY", 30)
                        
                        log.info(f"[GLOBAL] Waiting {delay} Seconds...")
                        self.global_delay(delay)
                        
                        log.info("[GLOBAL] Launching Packages...")
                        self.launch_all_packages()
                        
                        log.info("[GLOBAL] Recovery Completed. Resuming watchdog.")
                    finally:
                        self.exit_global_recovery()
                        self.recovery_lock.release()

            time.sleep(0.1)

    def launch_all_packages(self):
        for pkg in self.packages:
            self.launch_single_package(pkg)

    def launch_single_package(self, pkg):
        try:
            log.info(f"LAUNCH: Memulai {pkg} tanpa delay tambahan...")
            clean_package_cache(pkg)
            self.stats[pkg]['status'] = 'LOADING'
            
            pkg_intent = self.intent_url[pkg] if isinstance(self.intent_url, dict) else self.intent_url
            success = launch_and_wait(pkg, pkg_intent, self.timeout_seconds)
            
            if not success:
                try:
                    from core.autologin import run as run_autologin
                    self.stats[pkg]['status'] = 'LOGIN'
                    
                    login_status = run_autologin(pkg)
                    
                    if login_status in ["SUCCESS", "ALREADY_LOGGED_IN"]:
                        self.stats[pkg]['status'] = 'LOADING'
                        success = launch_and_wait(pkg, pkg_intent, self.timeout_seconds)
                    elif login_status == "CAPTCHA":
                        self.stats[pkg]['status'] = 'CAPTCHA'
                        return
                    else:
                        self.stats[pkg]['status'] = 'LOGIN FAILED'
                        return
                except ImportError:
                    pass

            current_time = time.time()
            
            if success:
                new_pid = get_pid(pkg)
                self.tracked_pids[pkg] = new_pid
                self.stats[pkg]['pid'] = new_pid if new_pid else '-'
                self.stats[pkg]['recovery_count'] += 1
                self.stats[pkg]['status'] = 'ONLINE'
                self.stats[pkg]['uptime_start'] = current_time
                self.stats[pkg]['last_recovery_time'] = current_time
                self.stats[pkg]['consecutive_crashes'] = 0
                
                if self.config_data and self.config_data.get('GRID_ENABLED'):
                    try:
                        from core import gridlayout
                        gridlayout.apply_grid_single(
                            pkg, self.packages,
                            cell_w=self.config_data.get('GRID_CELL_W') or None,
                            cell_h=self.config_data.get('GRID_CELL_H') or None,
                            cols=self.config_data.get('GRID_COLS') or None,
                            margin=self.config_data.get('GRID_MARGIN', 10),
                            offset_y=self.config_data.get('GRID_OFFSET_Y', 60),
                        )
                    except ImportError:
                        pass
            else:
                if self.stats[pkg]['status'] not in ['LOGIN FAILED', 'CAPTCHA']:
                    self.stats[pkg]['status'] = 'FAILED'
        except Exception as e:
            log.error(f"LAUNCH FATAL: {str(e)}")
            self.stats[pkg]['status'] = 'FAILED'

    def single_recovery_worker(self, pkg):
        """Hanya dipanggil oleh Watchdog saat mendeteksi crash tunggal (PID hilang)"""
        try:
            log.info(f"[SINGLE] Crash detected for {pkg}. Waiting 15 seconds...")
            time.sleep(15)
            self.launch_single_package(pkg)
        except Exception as e:
            log.error(f"SINGLE RECOVERY FATAL: {str(e)}")
            self.stats[pkg]['status'] = 'FAILED'

_manager = RecoveryManager()

def start_recovery_manager(packages, stats, tracked_pids, intent_url, timeout_seconds, config_data):
    _manager.configure(packages, stats, tracked_pids, intent_url, timeout_seconds, config_data)
    _manager.start()

def stop_recovery_manager():
    _manager.stop()

def trigger_recovery(pkg):
    threading.Thread(
        target=_manager.single_recovery_worker,
        args=(pkg,),
        daemon=True
    ).start()

def is_global_recovery():
    return _manager.watchdog_paused()

def get_global_state():
    return _manager.get_global_state()
        
