# Latest BDS integration test

- Lab commit: `deb638fa4db763937c6c1811ddf51a6b6bb4d070`
- Lab Actions: [33243106215](https://github.com/ReallocAll/bds-test-lab/actions/runs/33243106215)
- State: **PASS**
- Spark SHA: `c14e2897b6a956437dcfccd1cf9e531fac6b8bb0`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-29T08:57:18.542293Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/eLU09zLwR0) | [viewer](https://spark.lucko.me/C8CSp8txqn) | [viewer](https://spark.lucko.me/icg4YL6xIS) |

- windows soak RSS: start `617738240`, end `57110528`, peak `619487232`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/ktIUXyr3TM) | [viewer](https://spark.lucko.me/yk3uAiurcv) | [viewer](https://spark.lucko.me/lKeZ8Rq5o5) |

- linux soak RSS: start `853667840`, end `860094464`, peak `860094464`
- linux soak threads: start `27`, end `28`, peak `28`
