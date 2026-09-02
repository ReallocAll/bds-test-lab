# Latest BDS integration test

- Lab commit: `7118a0f47bf08dd648ca5b5e432e824533e84a98`
- Lab Actions: [33619260327](https://github.com/ReallocAll/bds-test-lab/actions/runs/33619260327)
- State: **FAIL**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T10:26:02.664470Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Gdvmw6nFBh) | [viewer](https://spark.lucko.me/D6Mf6XyhN5) | [viewer](https://spark.lucko.me/dU6uVwk8Yk) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
