# Latest BDS integration test

- Lab commit: `94c244862bc91a3862ee5613d35ceb900d191c3c`
- Lab Actions: [33312017899](https://github.com/ReallocAll/bds-test-lab/actions/runs/33312017899)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T13:10:31.686559Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/xJW7bWncJu) | [viewer](https://spark.lucko.me/ovY0uYscLP) | [viewer](https://spark.lucko.me/jDGAJJhBe4) |

- windows soak RSS: start `617730048`, end `56422400`, peak `618680320`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/USpvNhbwGj) | [viewer](https://spark.lucko.me/iwaCSFY78y) | [viewer](https://spark.lucko.me/BfrCgxtJQ2) |

- linux soak RSS: start `850870272`, end `856363008`, peak `856363008`
- linux soak threads: start `27`, end `28`, peak `28`
