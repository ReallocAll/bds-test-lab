# Latest BDS integration test

- Lab commit: `2e57a1cc932cc09bfa2eaaca5010bbddaa823196`
- Lab Actions: [33295280819](https://github.com/ReallocAll/bds-test-lab/actions/runs/33295280819)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T06:14:41.321581Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/AukWpbzEqc) | [viewer](https://spark.lucko.me/1QXTJSi0ZB) | [viewer](https://spark.lucko.me/6LZYkk9Fay) |

- windows soak RSS: start `618156032`, end `53301248`, peak `619208704`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/1ZBviXng4q) | [viewer](https://spark.lucko.me/Z509FnFmqc) | [viewer](https://spark.lucko.me/UaPfLdGPL5) |

- linux soak RSS: start `848560128`, end `854401024`, peak `854401024`
- linux soak threads: start `27`, end `28`, peak `28`
