# Latest BDS integration test

- Lab commit: `ad1bf7237b3231a13dc02bd63ebc691b891c0339`
- Lab Actions: [32980310193](https://github.com/ReallocAll/bds-test-lab/actions/runs/32980310193)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `27cc2e04d843bd70f089b0814ddba3054d4c55ef`
- Completed: `2026-08-26T14:27:51.325178Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **running** | `26.44` | `not_started` | `not_started` | `30m` | [viewer](https://spark.lucko.me/44hGKjhLmh) |  |  |
