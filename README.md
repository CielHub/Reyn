# CARRERA-HUB Regression Stage 3B

Baseline inheritance: Stage 3A (Target Resolver + State Machine + Smart Join Verification).

## Added in Stage 3B

**Recovery Terminal Reset only.** Background recovery workers never write to the terminal. They set a one-shot reset event, and the existing dashboard renderer consumes it and performs `console.clear()` exactly once on the next dashboard frame.

Reset requests are generated when:
- single-package watchdog recovery is triggered;
- global recovery (memory/error event) begins.

## Explicitly unchanged

- Target Resolver
- State Machine
- Smart Join Verification
- Dashboard layout/renderer architecture
- Rich Live lifecycle
- Watchdog timing/logic except the reset request hook
- Recovery launch/kill behavior
- RAM Guard / Cache Cleaner

This is a regression experiment, not a baseline update.
