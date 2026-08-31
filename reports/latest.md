# Latest BDS integration test

- Lab commit: `eaa69b60227bb3ba95a24a1fb88e7a443a175ffa`
- Lab Actions: [33380878372](https://github.com/ReallocAll/bds-test-lab/actions/runs/33380878372)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T10:38:37.753479Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/9gsmL1ydkD) | [viewer](https://spark.lucko.me/sPNU3FQ2iU) | [viewer](https://spark.lucko.me/4eeF0l8HEo) |

- windows soak RSS: start `617259008`, end `69046272`, peak `620683264`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/KCswAQZPwR) | [viewer](https://spark.lucko.me/U6xDpcG6Vu) | [viewer](https://spark.lucko.me/ws6INX3WTL) |

- linux soak RSS: start `851505152`, end `857591808`, peak `857591808`
- linux soak threads: start `27`, end `28`, peak `28`
