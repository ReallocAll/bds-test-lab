# Latest BDS integration test

- Lab commit: `08c7b56e7ea06320d0d4d749a58a4ff1f40d0685`
- Lab Actions: [32941670851](https://github.com/ReallocAll/bds-test-lab/actions/runs/32941670851)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `ad11276d1b9c0b7745acfdfefa7396169c2bcbc6`
- Completed: `2026-08-26T07:28:22.388728Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` | [viewer](https://spark.lucko.me/wNrr8pNAF6) |  |  |

**Windows error:** `RuntimeError: Allocation profiler produced no spark viewer URL`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Hw3OJsEdET) | [viewer](https://spark.lucko.me/2gSy5NprlR) | [viewer](https://spark.lucko.me/1yjwYA7aFQ) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
