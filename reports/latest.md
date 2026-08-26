# Latest BDS integration test

- Lab commit: `502ee70b86054eee164bb987ca46ae4145e2eb70`
- Lab Actions: [33011385807](https://github.com/ReallocAll/bds-test-lab/actions/runs/33011385807)
- State: **FAIL**
- Spark SHA: `bc280bf0a33c0ae3daedddf2739578b21082fff6`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T20:45:18.390684Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Bvp6SRiEOv) | [viewer](https://spark.lucko.me/eJdgvczLRU) | [viewer](https://spark.lucko.me/nnIfSUeK4R) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
