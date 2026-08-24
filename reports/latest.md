# Latest BDS integration test

- Lab commit: `1cd99da18ff3d576a64084aa8b1ababa1142ef7f`
- Lab Actions: [32786007745](https://github.com/ReallocAll/bds-test-lab/actions/runs/32786007745)
- State: **PASS**
- Spark SHA: `3fa44c25d1ae44d625d40e4038f1c274784bca1e`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Completed: `2026-08-24T23:15:11.566999Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/IJpHe1s3Xx) |  | [viewer](https://spark.lucko.me/QUv91wnmUM) |

- windows soak RSS: start `614326272`, end `46665728`, peak `615034880`
- windows soak threads: start `58`, end `42`, peak `58`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/DlbHJNXRTD) | [viewer](https://spark.lucko.me/iCYqwNpWLn) | [viewer](https://spark.lucko.me/OCeNh5Tm4R) |

- linux soak RSS: start `832000000`, end `838008832`, peak `838008832`
- linux soak threads: start `29`, end `30`, peak `30`
