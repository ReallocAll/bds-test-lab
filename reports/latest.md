# Latest BDS integration test

- Lab commit: `4298cfaa2618721ccafcb8fbebcab107b40826e4`
- Lab Actions: [33017905858](https://github.com/ReallocAll/bds-test-lab/actions/runs/33017905858)
- State: **PASS**
- Spark SHA: `6930eeadf404635369ee1969d7d076fa98af5593`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-26T22:33:23.474370Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/FsgnpuDuJC) | [viewer](https://spark.lucko.me/LvmopVLuS7) | [viewer](https://spark.lucko.me/1pXXUTPF7T) |

- windows soak RSS: start `615723008`, end `63946752`, peak `616079360`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/w6QyroWWyc) | [viewer](https://spark.lucko.me/TQx5svlMeZ) | [viewer](https://spark.lucko.me/JRgdugbeXD) |

- linux soak RSS: start `838905856`, end `844406784`, peak `844406784`
- linux soak threads: start `27`, end `28`, peak `28`
