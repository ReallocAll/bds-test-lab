# Latest BDS integration test

- Lab commit: `f9962dedc9056b16794dc8108d9f9dcd3736a10f`
- Lab Actions: [33359484605](https://github.com/ReallocAll/bds-test-lab/actions/runs/33359484605)
- State: **FAIL**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T05:40:40.994927Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/JPumDXvIZB) | [viewer](https://spark.lucko.me/aa1EUWrxbB) | [viewer](https://spark.lucko.me/PKVDTyOYgu) |

- linux soak RSS: start `850903040`, end `857001984`, peak `857001984`
- linux soak threads: start `27`, end `28`, peak `28`
