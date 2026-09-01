# Latest BDS integration test

- Lab commit: `d50be873f1dabe4065d758dfd7158940b941cf3c`
- Lab Actions: [33520388603](https://github.com/ReallocAll/bds-test-lab/actions/runs/33520388603)
- State: **FAIL**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-09-01T15:07:05.525850Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/MRyS62UHfT) | [viewer](https://spark.lucko.me/UcvjmFnFq1) | [viewer](https://spark.lucko.me/MNIj5Kup7n) |

- windows soak RSS: start `616570880`, end `53469184`, peak `617779200`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **FAIL** | `` | `` | `` | `m` |  |  |  |

**Linux error:** `No integration result was produced`
