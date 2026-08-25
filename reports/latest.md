# Latest BDS integration test

- Lab commit: `3be4678cdeb9eefdfa062b49869d8d98d0525dab`
- Lab Actions: [32842388599](https://github.com/ReallocAll/bds-test-lab/actions/runs/32842388599)
- State: **PASS**
- Spark SHA: `e2d26b11636f536bf6e5ead6c4a59a20372483c4`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Completed: `2026-08-25T11:59:55.819414Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/KVxuGy8BHN) |  | [viewer](https://spark.lucko.me/iaYc9kmpm4) |

- windows soak RSS: start `614473728`, end `52957184`, peak `615858176`
- windows soak threads: start `58`, end `42`, peak `58`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/XO0ZZoZ5Wi) | [viewer](https://spark.lucko.me/ZXrXOgHpgS) | [viewer](https://spark.lucko.me/GYpIVAR5d0) |

- linux soak RSS: start `755494912`, end `761565184`, peak `761565184`
- linux soak threads: start `29`, end `30`, peak `30`
