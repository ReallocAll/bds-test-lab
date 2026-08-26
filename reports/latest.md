# Latest BDS integration test

- Lab commit: `8e83ce8934a069241d551f02e64aee021f0ef8b4`
- Lab Actions: [33014228187](https://github.com/ReallocAll/bds-test-lab/actions/runs/33014228187)
- State: **FAIL**
- Spark SHA: `52dc0d4a4b04fefb8ced3758d1a283f731ec1bb5`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T21:14:46.157523Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `not_started` | `not_started` | `30m` | [viewer](https://spark.lucko.me/XjloUJWZM4) |  |  |
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/dfvnKvKIKA) | [viewer](https://spark.lucko.me/6bmpR8fbaJ) | [viewer](https://spark.lucko.me/0anxlb7Z4Z) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
