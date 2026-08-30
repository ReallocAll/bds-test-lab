# Latest BDS integration test

- Lab commit: `5be07f4c65f39327f98339d837030ddd79855483`
- Lab Actions: [33316455870](https://github.com/ReallocAll/bds-test-lab/actions/runs/33316455870)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T14:49:35.765609Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/KnWjz3KtNO) | [viewer](https://spark.lucko.me/q5R7dlstOA) | [viewer](https://spark.lucko.me/tHSzb4Mmgf) |

- windows soak RSS: start `618708992`, end `57090048`, peak `620154880`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Fw3k15kvyX) | [viewer](https://spark.lucko.me/uW69w1dnfD) | [viewer](https://spark.lucko.me/l2fXmdDzT9) |

- linux soak RSS: start `853241856`, end `858775552`, peak `858775552`
- linux soak threads: start `27`, end `28`, peak `28`
