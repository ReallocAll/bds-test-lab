# Latest BDS integration test

- Lab commit: `f120c7ff6c9ed11cf0adeb7cdaf4a55f70b4a484`
- Lab Actions: [33031497243](https://github.com/ReallocAll/bds-test-lab/actions/runs/33031497243)
- State: **PASS**
- Spark SHA: `b44840bf3595518ac143b07aa6f3552d92811801`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-27T02:24:59.732470Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/0rB7Yh2Fb5) | [viewer](https://spark.lucko.me/q1zJkPHAuu) | [viewer](https://spark.lucko.me/ZggdC4QGDr) |

- windows soak RSS: start `615825408`, end `49319936`, peak `617795584`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/a73QnMYcg3) | [viewer](https://spark.lucko.me/dQe7czPotW) | [viewer](https://spark.lucko.me/Ke9nxhWtYK) |

- linux soak RSS: start `837816320`, end `842743808`, peak `842743808`
- linux soak threads: start `27`, end `28`, peak `28`
