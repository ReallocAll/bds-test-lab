# Latest BDS integration test

- Lab commit: `7c9ba8770abde66152e0454cf8b29569045ff6db`
- Lab Actions: [32944598234](https://github.com/ReallocAll/bds-test-lab/actions/runs/32944598234)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `ad11276d1b9c0b7745acfdfefa7396169c2bcbc6`
- Completed: `2026-08-26T08:21:04.776689Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/POOyavWzye) | [viewer](https://spark.lucko.me/bvguwi6Ftg) | [viewer](https://spark.lucko.me/952LINiveR) |

- linux soak RSS: start `832290816`, end `837660672`, peak `837660672`
- linux soak threads: start `29`, end `30`, peak `30`
