# Latest BDS integration test

- Lab commit: `99cf3253406bacf0899de1910a03eb29827631a8`
- Lab Actions: [33511621269](https://github.com/ReallocAll/bds-test-lab/actions/runs/33511621269)
- State: **PASS**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-09-01T13:40:47.156713Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/WIzrhscY0G) | [viewer](https://spark.lucko.me/Z8IanWRG9j) | [viewer](https://spark.lucko.me/1LYhywwi2T) |

- windows soak RSS: start `619073536`, end `54800384`, peak `619966464`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/dOqP1sqFu9) | [viewer](https://spark.lucko.me/Jxntk9xi6A) | [viewer](https://spark.lucko.me/sFMKcKChVv) |

- linux soak RSS: start `852901888`, end `858636288`, peak `858636288`
- linux soak threads: start `27`, end `28`, peak `28`
