# Latest BDS integration test

- Lab commit: `5b6c6824dfb2f8c9fa22bd0b2eb87284aa80bb7e`
- Lab Actions: [33015112350](https://github.com/ReallocAll/bds-test-lab/actions/runs/33015112350)
- State: **FAIL**
- Spark SHA: `52dc0d4a4b04fefb8ced3758d1a283f731ec1bb5`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T21:31:24.286849Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/WMe29OP56z) | [viewer](https://spark.lucko.me/lqWhXcciLt) | [viewer](https://spark.lucko.me/NnAs5vUMK1) |

- windows soak RSS: start `None`, end `None`, peak `None`
- windows soak threads: start `None`, end `None`, peak `None`
| Linux | **FAIL** | `26.44` | `forced_after_failure` | `crashed_waiting_restart` | `30m` | [viewer](https://spark.lucko.me/ugapfF72UJ) | [viewer](https://spark.lucko.me/M8nUR7XUJ3) |  |

**Linux error:** `TimeoutError: Timed out after 90s waiting for Spark crash recovery replay`
