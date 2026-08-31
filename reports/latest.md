# Latest BDS integration test

- Lab commit: `9353234880e47cf7506eb6238dd2fee7ec64e1f0`
- Lab Actions: [33393822548](https://github.com/ReallocAll/bds-test-lab/actions/runs/33393822548)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-31T13:23:05.827730Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/2g7u5bLsZ1) | [viewer](https://spark.lucko.me/9nuz2dnxkG) | [viewer](https://spark.lucko.me/86QG0jLpk4) |

- windows soak RSS: start `618024960`, end `54743040`, peak `619532288`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/eB2tppZ9jf) | [viewer](https://spark.lucko.me/sKejGHPtBL) | [viewer](https://spark.lucko.me/nu1N72Xwb9) |

- linux soak RSS: start `852336640`, end `857784320`, peak `857784320`
- linux soak threads: start `27`, end `28`, peak `28`
