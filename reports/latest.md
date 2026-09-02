# Latest BDS integration test

- Lab commit: `d8b6d3888a33ae44b5041536778968a522d80297`
- Lab Actions: [33652713459](https://github.com/ReallocAll/bds-test-lab/actions/runs/33652713459)
- State: **PASS**
- Spark SHA: `8cb5c2b651a8c9808da397e6957526f87c1fe712`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T16:42:20.467077Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/apABGyyufb) | [viewer](https://spark.lucko.me/rWQRW0yIxn) | [viewer](https://spark.lucko.me/RopE68P7c9) |

- windows soak RSS: start `620175360`, end `66932736`, peak `621056000`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/IfDZ9BR0OT) | [viewer](https://spark.lucko.me/XOB3neY2Np) | [viewer](https://spark.lucko.me/rPoVHyV82L) |

- linux soak RSS: start `855851008`, end `862523392`, peak `862523392`
- linux soak threads: start `27`, end `28`, peak `28`
