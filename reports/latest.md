# Latest BDS integration test

- Lab commit: `44e5a1d324ade1b15eb656846e75cbe115e265ea`
- Lab Actions: [32979626684](https://github.com/ReallocAll/bds-test-lab/actions/runs/32979626684)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `27cc2e04d843bd70f089b0814ddba3054d4c55ef`
- Completed: `2026-08-26T14:21:17.738777Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **running** | `26.44` | `not_started` | `not_started` | `30m` | [viewer](https://spark.lucko.me/zv0zGMw0EL) |  |  |
