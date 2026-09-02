# Latest BDS integration test

- Lab commit: `8d7ddf5f08c9b115449efda546d599640ab0b53a`
- Lab Actions: [33648793940](https://github.com/ReallocAll/bds-test-lab/actions/runs/33648793940)
- State: **FAIL**
- Spark SHA: `9a0838c7e68f334b1d73e8ebde0017f7dd90a939`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T15:31:05.641781Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` | [viewer](https://spark.lucko.me/co9JYwRJTE) |  |  |
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `crashed_waiting_restart` | `30m` | [viewer](https://spark.lucko.me/G6ZDB18EtW) | [viewer](https://spark.lucko.me/ZgQ3aCaF2C) |  |
