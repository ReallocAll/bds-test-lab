# Latest BDS integration test

- Lab commit: `83699517dadca18e331d9422b7d4ca7b5032cbba`
- Lab Actions: [33624981608](https://github.com/ReallocAll/bds-test-lab/actions/runs/33624981608)
- State: **PASS**
- Spark SHA: `653e6821a45f5b5de5e8d22671960c2024db18be`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T12:03:21.375560Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/Fr0WZxHjMH) | [viewer](https://spark.lucko.me/00JxAn8P6p) | [viewer](https://spark.lucko.me/NQud6tj1aA) |

- windows soak RSS: start `619302912`, end `54149120`, peak `619712512`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/oPY2DfFVCS) | [viewer](https://spark.lucko.me/Flh0s2sXdA) | [viewer](https://spark.lucko.me/kkThTMdNb0) |

- linux soak RSS: start `851914752`, end `857436160`, peak `857436160`
- linux soak threads: start `27`, end `28`, peak `28`
