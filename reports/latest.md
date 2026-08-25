# Latest BDS integration test

- Lab commit: `43db00166283719753f04b0a9a9c871ea34b36e2`
- Lab Actions: [32831908330](https://github.com/ReallocAll/bds-test-lab/actions/runs/32831908330)
- State: **PASS**
- Spark SHA: `e2d26b11636f536bf6e5ead6c4a59a20372483c4`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Completed: `2026-08-25T09:57:44.472023Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/0Ym7Xmpop9) |  | [viewer](https://spark.lucko.me/jmLlxkBhiG) |

- windows soak RSS: start `614424576`, end `51695616`, peak `616755200`
- windows soak threads: start `58`, end `43`, peak `58`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/GEK4Sq9Znx) | [viewer](https://spark.lucko.me/SzhgiCjTih) | [viewer](https://spark.lucko.me/ZI26dVn4Ww) |

- linux soak RSS: start `834256896`, end `840142848`, peak `840142848`
- linux soak threads: start `29`, end `30`, peak `30`
