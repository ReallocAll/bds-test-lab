# Latest BDS integration test

- Lab commit: `d2a64b76ccb226ddac7d655e23a2b857af098009`
- Lab Actions: [33620117509](https://github.com/ReallocAll/bds-test-lab/actions/runs/33620117509)
- State: **FAIL**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `['46eff9f125f52eac76472d84339ead8fbf51fcd2', 'e20c19b0db4b8af3b1e5d9a7b260539e872fcc98']`
- Completed: `2026-09-02T10:36:04.883020Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **FAIL** | `26.33` | `not_started` | `not_started` | `30m` |  |  |  |

**Linux error:** `RuntimeError: Server exited while running console command: spark tps`
