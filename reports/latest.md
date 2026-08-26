# Latest BDS integration test

- Lab commit: `9d54380edd6d1bd14d42e4edabd566b29f6cb1cc`
- Lab Actions: [33020675947](https://github.com/ReallocAll/bds-test-lab/actions/runs/33020675947)
- State: **PASS**
- Spark SHA: `6930eeadf404635369ee1969d7d076fa98af5593`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T23:17:14.538741Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/y6WWTLjUvn) | [viewer](https://spark.lucko.me/BPIT3SJdWg) | [viewer](https://spark.lucko.me/tF5LUpkefJ) |

- windows soak RSS: start `616648704`, end `51011584`, peak `617938944`
- windows soak threads: start `56`, end `41`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/fJmZP4xffa) | [viewer](https://spark.lucko.me/5r2j6luxXh) | [viewer](https://spark.lucko.me/uBffDlAklS) |

- linux soak RSS: start `760930304`, end `766455808`, peak `766455808`
- linux soak threads: start `27`, end `28`, peak `28`
