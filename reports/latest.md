# Latest BDS integration test

- Lab commit: `4be40410df607bb03b9dcdb0674d6e337523f22f`
- Lab Actions: [33405948800](https://github.com/ReallocAll/bds-test-lab/actions/runs/33405948800)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T15:32:42.044483Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/ptBSG1lSGV) | [viewer](https://spark.lucko.me/bUyEsv8qs0) | [viewer](https://spark.lucko.me/dLpp6erwUh) |

- windows soak RSS: start `617164800`, end `55300096`, peak `620158976`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/XYEVjB1LGp) | [viewer](https://spark.lucko.me/uxrlLiKS4X) | [viewer](https://spark.lucko.me/yybtmrJ31R) |

- linux soak RSS: start `848621568`, end `854892544`, peak `854892544`
- linux soak threads: start `27`, end `28`, peak `28`
