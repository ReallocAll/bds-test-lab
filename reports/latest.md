# Latest BDS integration test

- Lab commit: `093914825c50d0d64bd5040af88a8f7921284320`
- Lab Actions: [32987830010](https://github.com/ReallocAll/bds-test-lab/actions/runs/32987830010)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `27cc2e04d843bd70f089b0814ddba3054d4c55ef`
- Completed: `2026-08-26T16:22:42.986869Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/NslCBKeQl8) | [viewer](https://spark.lucko.me/4pQpdEgp6p) | [viewer](https://spark.lucko.me/92IbQ8P5nA) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
