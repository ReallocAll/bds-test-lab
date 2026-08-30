# Latest BDS integration test

- Lab commit: `02081ca00d0ec14353e01a9eac2518d334d3aa63`
- Lab Actions: [33309513106](https://github.com/ReallocAll/bds-test-lab/actions/runs/33309513106)
- State: **FAIL**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T11:51:09.102495Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/ZdJowzZhnj) | [viewer](https://spark.lucko.me/bpndZrbeca) | [viewer](https://spark.lucko.me/lvYFoy8M2l) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
