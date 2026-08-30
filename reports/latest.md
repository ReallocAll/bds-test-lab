# Latest BDS integration test

- Lab commit: `743d05ba55ad9f470d51a0e78cc6f83a5e781247`
- Lab Actions: [33309976625](https://github.com/ReallocAll/bds-test-lab/actions/runs/33309976625)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T12:23:54.450291Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/zy8RvTV0EY) | [viewer](https://spark.lucko.me/eldjYSGfFt) | [viewer](https://spark.lucko.me/DjMzsZE2r7) |

- windows soak RSS: start `617930752`, end `60477440`, peak `618893312`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/a9PIbdFaj2) | [viewer](https://spark.lucko.me/fS0ZmhavKh) | [viewer](https://spark.lucko.me/asiHPP6hFG) |

- linux soak RSS: start `850804736`, end `857206784`, peak `857206784`
- linux soak threads: start `27`, end `28`, peak `28`
