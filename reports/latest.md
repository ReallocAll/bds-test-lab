# Latest BDS integration test

- Lab commit: `cf03ae36f07847304b0326b9d0e803cc36df2a19`
- Lab Actions: [33446431856](https://github.com/ReallocAll/bds-test-lab/actions/runs/33446431856)
- State: **FAIL**
- Spark SHA: `d101ee43db32e8d9ed4194a691410093e2ac90d5`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T23:00:50.244743Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/tFZzsqpQwE) | [viewer](https://spark.lucko.me/OnPOwQ5cIc) | [viewer](https://spark.lucko.me/94pdTY6whe) |

- linux soak RSS: start `851472384`, end `858275840`, peak `858275840`
- linux soak threads: start `27`, end `28`, peak `28`
