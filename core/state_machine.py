"""Minimal package state transition helper for regression Stage 2."""
from core.states import PackageState
from core.logger import log

_ALLOWED = {
    PackageState.OFFLINE: {PackageState.LAUNCHING, PackageState.NO_TARGET},
    PackageState.LAUNCHING: {PackageState.CONNECTING, PackageState.ONLINE, PackageState.FAILED, PackageState.NO_TARGET},
    PackageState.CONNECTING: {PackageState.ONLINE, PackageState.FAILED, PackageState.RECOVERING},
    PackageState.ONLINE: {PackageState.OFFLINE, PackageState.RECOVERING, PackageState.COOLDOWN},
    PackageState.RECOVERING: {PackageState.LAUNCHING, PackageState.FAILED, PackageState.COOLDOWN},
    PackageState.COOLDOWN: {PackageState.LAUNCHING, PackageState.OFFLINE},
    PackageState.FAILED: {PackageState.RECOVERING, PackageState.COOLDOWN, PackageState.LAUNCHING},
    PackageState.NO_TARGET: {PackageState.LAUNCHING, PackageState.OFFLINE},
}

def set_state(stats, pkg, state):
    new_value = state.value if isinstance(state, PackageState) else str(state)
    old_value = stats[pkg].get("status")
    old_state = next((s for s in PackageState if s.value == old_value), None)
    new_state = next((s for s in PackageState if s.value == new_value), None)
    if old_state and new_state and old_state != new_state and new_state not in _ALLOWED.get(old_state, set()):
        log.debug(f"STATE: {pkg}: {old_value} -> {new_value} (forced)")
    stats[pkg]["status"] = new_value
    stats[pkg]["state"] = new_value
