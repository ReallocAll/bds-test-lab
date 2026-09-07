# Latest BDS integration test

- Lab commit: `eb63f51cec4dfb6d71fca870bdffec66fab3a42d`
- Lab Actions: [34086059137](https://github.com/ReallocAll/bds-test-lab/actions/runs/34086059137)
- State: **PASS**
- Spark SHA: `8cb5c2b651a8c9808da397e6957526f87c1fe712`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-07T05:47:18.443616Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/LoVY9c85h4) | [viewer](https://spark.lucko.me/K79l6sYeMN) | [viewer](https://spark.lucko.me/XorjhrYXVw) |

- windows soak RSS: start `618876928`, end `58241024`, peak `621076480`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/2iTyHBxvZZ) | [viewer](https://spark.lucko.me/M6sVQWVuim) | [viewer](https://spark.lucko.me/i39QsbMcMK) |

- linux soak RSS: start `855932928`, end `861368320`, peak `861368320`
- linux soak threads: start `27`, end `28`, peak `28`
