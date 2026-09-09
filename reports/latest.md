# Latest BDS integration test

- Lab commit: `e625a0e24ad3b4cad0faa47a49203b7615222d18`
- Lab Actions: [34294830959](https://github.com/ReallocAll/bds-test-lab/actions/runs/34294830959)
- State: **FAIL**
- Spark SHA: `7d18dd0b70ea26d05a450c22a5e83bf2c2863968`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-09T00:55:57.726996Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/vaUu3WzEJb) | [viewer](https://spark.lucko.me/RKIak5aifK) | [viewer](https://spark.lucko.me/5DUG3ehBoK) |

- linux soak RSS: start `857870336`, end `863313920`, peak `863313920`
- linux soak threads: start `27`, end `28`, peak `28`
