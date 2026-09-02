# Latest BDS integration test

- Lab commit: `ac4d19ca9fcf1955794caf6c8175b6c80ba7d91f`
- Lab Actions: [33647496720](https://github.com/ReallocAll/bds-test-lab/actions/runs/33647496720)
- State: **FAIL**
- Spark SHA: `9a0838c7e68f334b1d73e8ebde0017f7dd90a939`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T15:21:08.570249Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/rka1Tnlw9Q) | [viewer](https://spark.lucko.me/EFZkPCNk0C) | [viewer](https://spark.lucko.me/mRs21hImIM) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
