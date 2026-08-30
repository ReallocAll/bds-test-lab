# Latest BDS integration test

- Lab commit: `c972ceab335652525c467e0d17ca0d6c4c4b7c58`
- Lab Actions: [33330272387](https://github.com/ReallocAll/bds-test-lab/actions/runs/33330272387)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T19:45:37.341176Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/pwWhtZUstn) | [viewer](https://spark.lucko.me/ilUody78gT) | [viewer](https://spark.lucko.me/RZgUheYHmv) |

- windows soak RSS: start `617099264`, end `52264960`, peak `618897408`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/NusGLCvhpK) | [viewer](https://spark.lucko.me/TWSc7bw77v) | [viewer](https://spark.lucko.me/j6v69PKSNW) |

- linux soak RSS: start `851001344`, end `857239552`, peak `857239552`
- linux soak threads: start `27`, end `28`, peak `28`
