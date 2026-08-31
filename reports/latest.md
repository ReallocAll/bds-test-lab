# Latest BDS integration test

- Lab commit: `75d2f7ee9f08297feec158a66620c129b54f50b3`
- Lab Actions: [33429849404](https://github.com/ReallocAll/bds-test-lab/actions/runs/33429849404)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T19:50:42.947762Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/77zE2mAgfz) | [viewer](https://spark.lucko.me/qk6OcDc3Go) | [viewer](https://spark.lucko.me/DsWLVleYq0) |

- windows soak RSS: start `615428096`, end `54444032`, peak `617467904`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/ipTDV8rNTC) | [viewer](https://spark.lucko.me/cCZpnuY0eN) | [viewer](https://spark.lucko.me/MQetUwZDU0) |

- linux soak RSS: start `851730432`, end `857489408`, peak `857489408`
- linux soak threads: start `27`, end `28`, peak `28`
