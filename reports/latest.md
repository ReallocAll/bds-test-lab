# Latest BDS integration test

- Lab commit: `ada7545c482468173d1cb1f4366310752298e869`
- Lab Actions: [33002855686](https://github.com/ReallocAll/bds-test-lab/actions/runs/33002855686)
- State: **FAIL**
- Spark SHA: `3190b9fe5ee1c8eb28a272e73df3c6dc9beee1ad`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T19:32:18.142150Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `crashed_waiting_restart` | `30m` | [viewer](https://spark.lucko.me/eQlIHoUdcC) | [viewer](https://spark.lucko.me/jw447k4MLd) |  |

**Windows error:** `TimeoutError: Timed out after 90s waiting for Spark crash recovery replay`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/pi1BdcaExQ) | [viewer](https://spark.lucko.me/6r0BIwvPsF) | [viewer](https://spark.lucko.me/mup5mMTuGi) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
