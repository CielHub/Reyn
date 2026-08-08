# CARRERA-HUB Stage 3A

Regression test build based directly on **Stage 2 - State Machine**.

## Added in this stage

Only one functional change was introduced:

- `core/join_verifier.py`
- `core/launcher.py` performs a post-launch verification after the existing PID check.

The verifier checks:

1. Roblox process is still alive via `pidof`.
2. Android foreground/focus information when available.
3. A floating Delta Lite window is not treated as a failure when the process remains alive.

## Explicitly NOT changed

- Dashboard renderer
- Rich `Live` lifecycle
- `screen=True` / `screen=False` settings
- Terminal reset behavior
- Console logging behavior
- Recovery manager
- State machine
- Target resolver
- Watchdog
- RAM guard
- Cache cleaner

## Regression goal

Run the same force-close test used on Stage 2. If Stage 2 is stable and Stage 3A becomes unstable, Smart Join Verification or its launcher integration becomes the primary suspect.

Do not use this build as the baseline until the user confirms the regression test passes.
