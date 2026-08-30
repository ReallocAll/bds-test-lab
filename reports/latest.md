# Latest BDS integration test

- Lab commit: `ccb028ae6e8d075b56e68591239f0b65374ecfeb`
- Lab Actions: [33306584273](https://github.com/ReallocAll/bds-test-lab/actions/runs/33306584273)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T11:01:06.037077Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/UTrZLQRSrT) | [viewer](https://spark.lucko.me/Rz553geq2R) | [viewer](https://spark.lucko.me/BgeYWAuILl) |

- windows soak RSS: start `617242624`, end `66027520`, peak `618561536`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/ukg3xkriWV) | [viewer](https://spark.lucko.me/tQHcrVasYV) | [viewer](https://spark.lucko.me/1uFFZg9We6) |

- linux soak RSS: start `850210816`, end `856821760`, peak `856821760`
- linux soak threads: start `27`, end `28`, peak `28`
