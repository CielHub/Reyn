# CARRERA-HUB - Regression Stage 4

This build is based on **Stage 3B - Startup Full Terminal Reset**.

## Stage 4 addition

Stage 4 adds a three-tier recovery chain for **single-package crash recovery**:

1. **Tier 1 - Relaunch**
   - Relaunch the package directly.
   - Does not clean cache.
   - Does not perform a hard force-stop.

2. **Tier 2 - Cache Recovery**
   - Clean the package cache/code_cache.
   - Relaunch the package.

3. **Tier 3 - Hard Recovery**
   - Android `am force-stop` through root.
   - Clean package cache/code_cache.
   - Relaunch the package.

The chain stops as soon as a verified launch succeeds.

## Regression isolation

This stage intentionally does **not** modify:

- Dashboard renderer
- Rich Live configuration
- Startup full terminal reset
- Smart Join Verification
- Target Resolver
- State Machine
- Anti Recovery Loop
- Startup Health Check
- Global Recovery flow

Global recovery remains unchanged for this stage. The new tiered logic is isolated to the existing single-package watchdog recovery path.
