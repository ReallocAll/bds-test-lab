# Latest BDS integration test

- Lab commit: `849dabdc56f82a873e9736f88aeb10a97b0ae871`
- Lab Actions: [33118585782](https://github.com/ReallocAll/bds-test-lab/actions/runs/33118585782)
- State: **PASS**
- Spark SHA: `6a32a1d05ca50ddb106a0fdcfa0a1762de018acd`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-27T22:04:12.978314Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/hA5pSoy3UT) | [viewer](https://spark.lucko.me/E0fFQaxoLZ) | [viewer](https://spark.lucko.me/yS61MGJxkz) |

- windows soak RSS: start `615813120`, end `67784704`, peak `617529344`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/GgFjpNNTPo) | [viewer](https://spark.lucko.me/dEvqINBzfi) | [viewer](https://spark.lucko.me/GN79olAHCB) |

- linux soak RSS: start `845676544`, end `851697664`, peak `851697664`
- linux soak threads: start `27`, end `28`, peak `28`
