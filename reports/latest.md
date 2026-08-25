# Latest BDS integration test

- Lab commit: `45e2b0a1864d87d438d34b68696aeadc48b3182d`
- Lab Actions: [32853807410](https://github.com/ReallocAll/bds-test-lab/actions/runs/32853807410)
- State: **PASS**
- Spark SHA: `8ba85ecb9d0da76100af5bc8d2ead086f370e74e`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Completed: `2026-08-25T14:03:01.136234Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/mdIUtt9Ooq) |  | [viewer](https://spark.lucko.me/flD1qxWwu8) |

- windows soak RSS: start `613629952`, end `49053696`, peak `614830080`
- windows soak threads: start `58`, end `42`, peak `58`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/JLTpb0Q7Tp) | [viewer](https://spark.lucko.me/9yrce8Grqs) | [viewer](https://spark.lucko.me/gdM2GROBFX) |

- linux soak RSS: start `756219904`, end `761597952`, peak `761597952`
- linux soak threads: start `29`, end `30`, peak `30`
