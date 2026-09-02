# Latest BDS integration test

- Lab commit: `2859bc24fbcee0acfdc94f1c58fe04ba22cefe42`
- Lab Actions: [33651821359](https://github.com/ReallocAll/bds-test-lab/actions/runs/33651821359)
- State: **FAIL**
- Spark SHA: `8cb5c2b651a8c9808da397e6957526f87c1fe712`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T16:01:49.276590Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.45` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/1AtL6SEwKe) | [viewer](https://spark.lucko.me/SZ3rbC5pbd) | [viewer](https://spark.lucko.me/S3lsGUwy82) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
