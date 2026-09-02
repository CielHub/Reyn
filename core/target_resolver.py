"""Resolve the effective Roblox join target for each package."""
from dataclasses import dataclass
from core.deeplink import get_intent_url, get_lobby_intent

@dataclass(frozen=True)
class JoinTarget:
    target_type: str
    value: str
    intent_url: str
    scope: str

class TargetResolver:
    def __init__(self, config):
        self.config = config or {}

    def _lobby_only_enabled(self):
        raw = self.config.get("LOBBY_ONLY_MODE", 0)
        try:
            return int(raw) == 1
        except (TypeError, ValueError):
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def resolve(self, pkg):
        # Mode Lobby Only bersifat mutlak: begitu aktif, package TIDAK akan
        # pernah join Place ID / Private Server manapun, apapun konfigurasinya.
        if self._lobby_only_enabled():
            return JoinTarget("LOBBY", "", get_lobby_intent(), "GLOBAL")

        pkg_link = str(self.config.get(f"PKG_{pkg}", "") or "").strip()
        pkg_place = str(self.config.get(f"PKG_{pkg}_PLACE_ID", "") or "").strip()
        global_link = str(self.config.get("PRIVATE_SERVER_LINK", "") or "").strip()
        global_place = str(self.config.get("GLOBAL_PLACE_ID", "") or "").strip()

        # Config normalization guarantees exclusivity, but resolver remains defensive.
        if pkg_link:
            return JoinTarget("PRIVATE_SERVER", pkg_link, get_intent_url(pkg_link), "PACKAGE")
        if pkg_place:
            return JoinTarget("PLACE_ID", pkg_place, get_intent_url(pkg_place), "PACKAGE")
        if global_link:
            return JoinTarget("PRIVATE_SERVER", global_link, get_intent_url(global_link), "GLOBAL")
        if global_place:
            return JoinTarget("PLACE_ID", global_place, get_intent_url(global_place), "GLOBAL")
        return None

    def resolve_all(self, packages):
        return {pkg: self.resolve(pkg) for pkg in packages}
