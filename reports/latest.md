# Latest BDS integration test

- Lab commit: `1d5707be3c928be2df0cacded1b661468b40b291`
- Lab Actions: [33063087688](https://github.com/ReallocAll/bds-test-lab/actions/runs/33063087688)
- State: **PASS**
- Spark SHA: `c1ed938058ebfd3b417b50bc70c718e989eed60f`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-27T10:59:25.902825Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/oFHXUtGebc) | [viewer](https://spark.lucko.me/CVbgOvzetZ) | [viewer](https://spark.lucko.me/HJ3E8wI8Po) |

- windows soak RSS: start `616108032`, end `50892800`, peak `616869888`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/BxQKS1fAKu) | [viewer](https://spark.lucko.me/RRyDSgRwbI) | [viewer](https://spark.lucko.me/7ySoJZKzg0) |

- linux soak RSS: start `845856768`, end `851755008`, peak `851755008`
- linux soak threads: start `27`, end `28`, peak `28`
