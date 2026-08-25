# Latest BDS integration test

- Lab commit: `dd3d13be191e958a4242d8895c505eb00b51b57d`
- Lab Actions: [32825156377](https://github.com/ReallocAll/bds-test-lab/actions/runs/32825156377)
- State: **PASS**
- Spark SHA: `c94f30770fb003f275f4d00e1e42a392ac4da869`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Completed: `2026-08-25T08:41:03.698375Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/zuphTLoduT) |  | [viewer](https://spark.lucko.me/gHqaXClenI) |

- windows soak RSS: start `613683200`, end `50302976`, peak `615555072`
- windows soak threads: start `58`, end `43`, peak `58`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Gg8Z7CM8zd) | [viewer](https://spark.lucko.me/ICsDL16kLU) | [viewer](https://spark.lucko.me/pnwsJyHewm) |

- linux soak RSS: start `831897600`, end `837496832`, peak `837496832`
- linux soak threads: start `29`, end `30`, peak `30`
