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
import subprocess
from enum import Enum

from core.error_detector import has_event, get_event
from core.memory_guard import has_memory_event, get_memory_event, reset_memory_guard
from core.process_manager import graceful_kill, get_pid, hard_force_stop
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


    def _recovery_process_is_real(self, pkg, stable_seconds=4.0):
        """Strict recovery verification used ONLY by single-package recovery.

        A PID alone is not enough to declare Farming/ONLINE. The process must
        remain alive with the same PID for a short stability window and Android
        must expose a window/activity belonging to the target package. Delta
        Lite may be floating, so the package does not need to be the global
        foreground app.
        """
        deadline = time.time() + stable_seconds
        first_pid = get_pid(pkg)
        if not first_pid:
            return False, "PROCESS_NOT_RUNNING"

        saw_android_ui = False
        while time.time() < deadline:
            current_pid = get_pid(pkg)
            if not current_pid:
                return False, "PROCESS_DIED_DURING_VERIFY"
            if current_pid != first_pid:
                return False, "PID_CHANGED_DURING_VERIFY"

            if self._android_ui_mentions_package(pkg):
                saw_android_ui = True

            time.sleep(0.5)

        if not saw_android_ui:
            return False, "NO_PACKAGE_WINDOW_OR_ACTIVITY"

        return True, "PROCESS_STABLE_AND_UI_PRESENT"

    @staticmethod
    def _android_ui_mentions_package(pkg):
        """Best-effort check that Android exposes a UI/window for pkg."""
        commands = (
            ["dumpsys", "window", "windows"],
            ["dumpsys", "activity", "activities"],
        )
        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2,
                    errors="replace",
                )
                output = (result.stdout or "")
                if pkg in output:
                    return True
            except Exception:
                continue
        return False

    def _commit_recovery_success(self, pkg, level):
        """Commit ONLINE only after strict recovery verification succeeds."""
        verified, reason = self._recovery_process_is_real(pkg)
        if not verified:
            log.warning(f"[RECOVERY L{level}] {pkg} strict verification failed: {reason}")
            return False

        new_pid = get_pid(pkg)
        if not new_pid:
            log.warning(f"[RECOVERY L{level}] {pkg} PID disappeared after verification.")
            return False

        current_time = time.time()
        self.tracked_pids[pkg] = new_pid
        self.stats[pkg]['pid'] = new_pid
        self.stats[pkg]['status'] = 'ONLINE'
        self.stats[pkg]['uptime_start'] = current_time
        self.stats[pkg]['last_recovery_time'] = current_time
        self.stats[pkg]['recovery_count'] += 1
        self.stats[pkg]['consecutive_crashes'] = 0
        log.info(f"[RECOVERY L{level}] {pkg} recovered successfully: {reason}")
        return True

    def _launch_recovery_attempt(self, pkg, level):
        """Run one isolated recovery tier. Returns True only after a verified launch."""
        if level == 1:
            # Tier 1: relaunch only. No cache cleanup and no hard reset.
            log.info(f"[RECOVERY L1] Relaunching {pkg} without cache cleanup...")

        elif level == 2:
            # Tier 2: clean package cache, then relaunch.
            log.info(f"[RECOVERY L2] Cleaning cache for {pkg}, then relaunching...")
            clean_package_cache(pkg)

        elif level == 3:
            # Tier 3: hard-stop through Android package manager, clean cache, relaunch.
            log.info(f"[RECOVERY L3] Hard-stopping {pkg} + cache cleanup before relaunch...")
            hard_force_stop(pkg)
            time.sleep(1)
            clean_package_cache(pkg)

        else:
            return False

        self.stats[pkg]['status'] = 'LOADING'
        pkg_intent = self.intent_url[pkg] if isinstance(self.intent_url, dict) else self.intent_url
        if not pkg_intent:
            log.error(f"[RECOVERY L{level}] {pkg} has no valid Intent URL.")
            return False

        success = launch_and_wait(pkg, pkg_intent, self.timeout_seconds)

        # Preserve the existing auto-login fallback, but keep it inside the
        # current tier so a failed attempt can advance to the next tier.
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
                    return False
                else:
                    self.stats[pkg]['status'] = 'LOGIN FAILED'
                    return False
            except ImportError:
                pass

        if success:
            if self._commit_recovery_success(pkg, level):
                return True
            # The launcher returned success, but strict recovery verification
            # rejected it. Keep the visible state in RECOVERY while the next
            # tier is selected. Never expose a false Farming/ONLINE state.
            self.stats[pkg]['status'] = 'RECOVERY'
            self.stats[pkg]['pid'] = '-'
            log.warning(f"[RECOVERY L{level}] launch succeeded weakly but strict verification failed; advancing tier.")
            return False

        if self.stats[pkg]['status'] not in ['LOGIN FAILED', 'CAPTCHA']:
            self.stats[pkg]['status'] = 'FAILED'
        log.warning(f"[RECOVERY L{level}] {pkg} recovery attempt failed.")
        return False

    def single_recovery_worker(self, pkg):
        """Single-package tiered recovery: L1 -> L2 -> L3."""
        try:
            log.info(f"[SINGLE] Crash detected for {pkg}. Waiting 15 seconds...")
            time.sleep(15)

            for level in (1, 2, 3):
                # A prior tier may have restored the process while this worker
                # was preparing the next attempt. Do not relaunch unnecessarily.
                existing_pid = get_pid(pkg)
                if existing_pid:
                    if self._commit_recovery_success(pkg, level):
                        log.info(f"[RECOVERY] {pkg} was already healthy before Tier {level}; stopping recovery chain.")
                        return
                    log.info(f"[RECOVERY] {pkg} has a PID but strict verification failed; continuing Tier {level}.")

                if self._launch_recovery_attempt(pkg, level):
                    return

                if level < 3:
                    log.warning(f"[RECOVERY] {pkg} advancing from Tier {level} to Tier {level + 1}.")
                    time.sleep(2)

            log.error(f"[RECOVERY] {pkg} failed after all 3 recovery tiers.")
            self.stats[pkg]['status'] = 'FAILED'
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
        
