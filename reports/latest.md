# Latest BDS integration test

- Lab commit: `bbfc1c081a4446ccba4458eeba6789f5bfd61ef5`
- Lab Actions: [33225699767](https://github.com/ReallocAll/bds-test-lab/actions/runs/33225699767)
- State: **PASS**
- Spark SHA: `c14e2897b6a956437dcfccd1cf9e531fac6b8bb0`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-29T01:43:20.193669Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/C4GftkXiYJ) | [viewer](https://spark.lucko.me/H9BIYBkXVH) | [viewer](https://spark.lucko.me/e2s0ciTq5l) |

- windows soak RSS: start `615849984`, end `56217600`, peak `618459136`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/FdQMAVQpvd) | [viewer](https://spark.lucko.me/83GNwiVfB4) | [viewer](https://spark.lucko.me/TGCMQTiZO4) |

- linux soak RSS: start `852455424`, end `857407488`, peak `857407488`
- linux soak threads: start `27`, end `28`, peak `28`
