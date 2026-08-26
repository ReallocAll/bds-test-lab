# Latest BDS integration test

- Lab commit: `4e4941dd0dddd4745c70e4be0c603a0e8c28c6d1`
- Lab Actions: [33015734303](https://github.com/ReallocAll/bds-test-lab/actions/runs/33015734303)
- State: **FAIL**
- Spark SHA: `6930eeadf404635369ee1969d7d076fa98af5593`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T21:33:45.518214Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/v5CgGswBLi) | [viewer](https://spark.lucko.me/2NYTPua2gG) | [viewer](https://spark.lucko.me/FAE13gWWAp) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
