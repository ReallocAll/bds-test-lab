# Latest BDS integration test

- Lab commit: `53c5ebfe954ba3d46ce572030a4df2350e6eb101`
- Lab Actions: [33008818921](https://github.com/ReallocAll/bds-test-lab/actions/runs/33008818921)
- State: **FAIL**
- Spark SHA: `bc280bf0a33c0ae3daedddf2739578b21082fff6`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T20:38:28.328759Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/MUIWeHVWQ3) | [viewer](https://spark.lucko.me/sUge7YGKDO) | [viewer](https://spark.lucko.me/ZAkt5ii8zX) |

- windows soak RSS: start `None`, end `None`, peak `None`
- windows soak threads: start `None`, end `None`, peak `None`
| Linux | **FAIL** | `26.44` | `forced_after_failure` | `crashed_waiting_restart` | `30m` | [viewer](https://spark.lucko.me/TUlJnNa1hb) | [viewer](https://spark.lucko.me/fJGMRKW3Sd) |  |

**Linux error:** `TimeoutError: Timed out after 90s waiting for Spark crash recovery replay`
