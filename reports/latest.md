# Latest BDS integration test

- Lab commit: `515ef1ca22266e1e736bbbe91fab67f71b561366`
- Lab Actions: [33027811795](https://github.com/ReallocAll/bds-test-lab/actions/runs/33027811795)
- State: **FAIL**
- Spark SHA: `c5c04acd087dc596a918a72f51284926ed2b0097`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-27T01:16:05.011329Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/tPEk8RM7NX) | [viewer](https://spark.lucko.me/ag6NErHglU) | [viewer](https://spark.lucko.me/klWRpynwh4) |

- windows soak RSS: start `None`, end `None`, peak `None`
- windows soak threads: start `None`, end `None`, peak `None`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/YkEmvouFpq) | [viewer](https://spark.lucko.me/lr9oTtXYH1) | [viewer](https://spark.lucko.me/asJ4Uc2pL7) |

- linux soak RSS: start `841834496`, end `846823424`, peak `846823424`
- linux soak threads: start `27`, end `28`, peak `28`
