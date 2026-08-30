# Latest BDS integration test

- Lab commit: `3202a736cf05b2f47e41fe67aa20245721069300`
- Lab Actions: [33341660637](https://github.com/ReallocAll/bds-test-lab/actions/runs/33341660637)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T23:54:55.184542Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/11UZYVmDix) | [viewer](https://spark.lucko.me/u6q5StCoTR) | [viewer](https://spark.lucko.me/K11tn4VQW6) |

- windows soak RSS: start `617009152`, end `57311232`, peak `619020288`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/0PFieH8tIq) | [viewer](https://spark.lucko.me/xYU4CkroYt) | [viewer](https://spark.lucko.me/2rSGcyBGXq) |

- linux soak RSS: start `852668416`, end `858398720`, peak `858398720`
- linux soak threads: start `27`, end `28`, peak `28`
