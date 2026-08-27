# Latest BDS integration test

- Lab commit: `2f4ca9b430de37c4258652eb41d2cb473926bd07`
- Lab Actions: [33067971212](https://github.com/ReallocAll/bds-test-lab/actions/runs/33067971212)
- State: **PASS**
- Spark SHA: `c1ed938058ebfd3b417b50bc70c718e989eed60f`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-27T12:06:42.976458Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/mDnGZxzxyt) | [viewer](https://spark.lucko.me/dQGhS2Swlb) | [viewer](https://spark.lucko.me/oHYCrJP68e) |

- windows soak RSS: start `615407616`, end `62963712`, peak `616615936`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/vSFuMHDAgN) | [viewer](https://spark.lucko.me/Q2gv8NgpEF) | [viewer](https://spark.lucko.me/QyFUDap0zS) |

- linux soak RSS: start `762122240`, end `767242240`, peak `767242240`
- linux soak threads: start `27`, end `28`, peak `28`
