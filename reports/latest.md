# Latest BDS integration test

- Lab commit: `0d55408c4abda4e0ed04b23f1e255784d1fafe57`
- Lab Actions: [34294324980](https://github.com/ReallocAll/bds-test-lab/actions/runs/34294324980)
- State: **FAIL**
- Spark SHA: `7d18dd0b70ea26d05a450c22a5e83bf2c2863968`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-09T00:24:14.474454Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/CX3CVdsaAW) | [viewer](https://spark.lucko.me/R4c9KtczRe) | [viewer](https://spark.lucko.me/MJSrKZrT31) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
