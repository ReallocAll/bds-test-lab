# Latest BDS integration test

- Lab commit: `cab4f1c4deed039e4d7b7aa163f91928114aec53`
- Lab Actions: [33297161720](https://github.com/ReallocAll/bds-test-lab/actions/runs/33297161720)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T07:05:46.048814Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/KMRK7fZRMo) | [viewer](https://spark.lucko.me/MJD79ABTIv) | [viewer](https://spark.lucko.me/k9iAxcXkfy) |

- windows soak RSS: start `619831296`, end `65478656`, peak `620388352`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/EjVbXVDrg2) | [viewer](https://spark.lucko.me/cGjk3iKl5K) | [viewer](https://spark.lucko.me/uSwCNuORWr) |

- linux soak RSS: start `852054016`, end `858058752`, peak `858058752`
- linux soak threads: start `27`, end `28`, peak `28`
