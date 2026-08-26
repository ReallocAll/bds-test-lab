# Latest BDS integration test

- Lab commit: `3906e4cad7b2f58090f96a550d62e12eefc95926`
- Lab Actions: [32979696485](https://github.com/ReallocAll/bds-test-lab/actions/runs/32979696485)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `['27cc2e04d843bd70f089b0814ddba3054d4c55ef', 'e20c19b0db4b8af3b1e5d9a7b260539e872fcc98']`
- Completed: `2026-08-26T14:22:32.998028Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **FAIL** | `26.33` | `not_started` | `not_started` | `30m` |  |  |  |

**Linux error:** `RuntimeError: Server exited while running console command: spark tps`
