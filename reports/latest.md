# Latest BDS integration test

- Lab commit: `ce8eddd80fe3098bbf2ab612455d74c02bf8a361`
- Lab Actions: [33546313554](https://github.com/ReallocAll/bds-test-lab/actions/runs/33546313554)
- State: **PASS**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `1417ab2acb6071b2ccd742b0655768cc4fcb65f9`
- Completed: `2026-09-01T19:25:41.604075Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/pX8NYp4p0a) | [viewer](https://spark.lucko.me/0Y1tmBSvBr) | [viewer](https://spark.lucko.me/SN8eowb16o) |

- windows soak RSS: start `617926656`, end `54566912`, peak `619986944`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/X5bditZqIi) | [viewer](https://spark.lucko.me/pUEvG90WSD) | [viewer](https://spark.lucko.me/NEtHB8AhM1) |

- linux soak RSS: start `848781312`, end `855203840`, peak `855203840`
- linux soak threads: start `27`, end `28`, peak `28`
