# Latest BDS integration test

- Lab commit: `5a3510eead8d29c1174b6aff85183380b7d33e9f`
- Lab Actions: [33627994595](https://github.com/ReallocAll/bds-test-lab/actions/runs/33627994595)
- State: **PASS**
- Spark SHA: `653e6821a45f5b5de5e8d22671960c2024db18be`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T12:37:08.745867Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/kTFcVTkkvg) | [viewer](https://spark.lucko.me/UXFOWxvvn6) | [viewer](https://spark.lucko.me/9Mn96DjDb8) |

- windows soak RSS: start `619147264`, end `53641216`, peak `620384256`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/2trKsmZ3qo) | [viewer](https://spark.lucko.me/2YFHjMpdDR) | [viewer](https://spark.lucko.me/XJewZ9BN0H) |

- linux soak RSS: start `856616960`, end `863158272`, peak `863158272`
- linux soak threads: start `27`, end `28`, peak `28`
