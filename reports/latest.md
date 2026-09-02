# Latest BDS integration test

- Lab commit: `b1e61aeb4050f0907c8239c7412b49e8d72bf288`
- Lab Actions: [33652192671](https://github.com/ReallocAll/bds-test-lab/actions/runs/33652192671)
- State: **FAIL**
- Spark SHA: `8cb5c2b651a8c9808da397e6957526f87c1fe712`
- Endstone SHA: `['46eff9f125f52eac76472d84339ead8fbf51fcd2', 'e20c19b0db4b8af3b1e5d9a7b260539e872fcc98']`
- Completed: `2026-09-02T16:03:25.541589Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **FAIL** | `26.33` | `not_started` | `not_started` | `30m` |  |  |  |

**Linux error:** `RuntimeError: Server exited while running console command: spark tps`
