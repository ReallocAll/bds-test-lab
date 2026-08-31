# Latest BDS integration test

- Lab commit: `a1decaf64e3feef3a99f866b2860720d523f20bd`
- Lab Actions: [33350849614](https://github.com/ReallocAll/bds-test-lab/actions/runs/33350849614)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T03:01:49.792953Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/uqKacQGxmc) | [viewer](https://spark.lucko.me/VyCR3Yp2Ax) | [viewer](https://spark.lucko.me/3OUJ3mxyIP) |

- windows soak RSS: start `617295872`, end `52662272`, peak `617963520`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/GKIVlQMZpQ) | [viewer](https://spark.lucko.me/1z76TXcR7E) | [viewer](https://spark.lucko.me/lgsFM0Ds5c) |

- linux soak RSS: start `852258816`, end `857722880`, peak `857722880`
- linux soak threads: start `27`, end `28`, peak `28`
