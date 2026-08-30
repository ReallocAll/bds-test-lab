# Latest BDS integration test

- Lab commit: `c46c32414dfb13a78423370f0014282b7192b4db`
- Lab Actions: [33326864903](https://github.com/ReallocAll/bds-test-lab/actions/runs/33326864903)
- State: **PASS**
- Spark SHA: `15b79e814ee6542f8a2382df09353e9c2009c8d1`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-30T18:33:29.918088Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/uyxH7XDrei) | [viewer](https://spark.lucko.me/KjGwXEg9bC) | [viewer](https://spark.lucko.me/wR6TqYOQNX) |

- windows soak RSS: start `616992768`, end `52563968`, peak `618835968`
- windows soak threads: start `57`, end `40`, peak `57`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/mNjOlpcetL) | [viewer](https://spark.lucko.me/qTdw6pkiAh) | [viewer](https://spark.lucko.me/QXxEDcXpxx) |

- linux soak RSS: start `853925888`, end `860868608`, peak `860868608`
- linux soak threads: start `27`, end `28`, peak `28`
