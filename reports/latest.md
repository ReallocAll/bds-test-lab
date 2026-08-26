# Latest BDS integration test

- Lab commit: `e63f4c8e65abf76030da9fc3bc2e2e412a2f78ce`
- Lab Actions: [32988007313](https://github.com/ReallocAll/bds-test-lab/actions/runs/32988007313)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `27cc2e04d843bd70f089b0814ddba3054d4c55ef`
- Completed: `2026-08-26T16:54:29.871619Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Q1NvBGWqQf) | [viewer](https://spark.lucko.me/v5qe7TlceX) | [viewer](https://spark.lucko.me/KO70YQDttS) |

- linux soak RSS: start `840241152`, end `845139968`, peak `845139968`
- linux soak threads: start `27`, end `28`, peak `28`
