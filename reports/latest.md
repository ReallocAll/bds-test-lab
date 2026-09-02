# Latest BDS integration test

- Lab commit: `3dde32384714134aa99c3135d881bf94fa9a16a6`
- Lab Actions: [33648616915](https://github.com/ReallocAll/bds-test-lab/actions/runs/33648616915)
- State: **FAIL**
- Spark SHA: `9a0838c7e68f334b1d73e8ebde0017f7dd90a939`
- Endstone SHA: `['46eff9f125f52eac76472d84339ead8fbf51fcd2', 'e20c19b0db4b8af3b1e5d9a7b260539e872fcc98']`
- Completed: `2026-09-02T15:29:16.410056Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **FAIL** | `26.33` | `not_started` | `not_started` | `30m` |  |  |  |

**Linux error:** `RuntimeError: Server exited while running console command: spark tps`
