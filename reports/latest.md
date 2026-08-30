# Latest BDS integration test

- Lab commit: `79a6e3193a97481c19dd2f167aeb636d62de1bf7`
- Lab Actions: [33295029866](https://github.com/ReallocAll/bds-test-lab/actions/runs/33295029866)
- State: **FAIL**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T05:42:39.733063Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/BqB6FQYnJI) | [viewer](https://spark.lucko.me/ph5uEYjiEi) | [viewer](https://spark.lucko.me/53g9jYprz4) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
