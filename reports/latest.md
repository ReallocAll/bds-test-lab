# Latest BDS integration test

- Lab commit: `7cd611669a875b19c8010d3817e98d81e1f1f06e`
- Lab Actions: [32980345973](https://github.com/ReallocAll/bds-test-lab/actions/runs/32980345973)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `27cc2e04d843bd70f089b0814ddba3054d4c55ef`
- Completed: `2026-08-26T14:59:28.361563Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/w7MRHvBZ8I) | [viewer](https://spark.lucko.me/S1lqlwHd3f) | [viewer](https://spark.lucko.me/SabrNzg6N9) |

- linux soak RSS: start `841003008`, end `846585856`, peak `846585856`
- linux soak threads: start `27`, end `28`, peak `28`
