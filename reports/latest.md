# Latest BDS integration test

- Lab commit: `1f6308d7cf8ce841dcf8d307559d11dd78c28afd`
- Lab Actions: [33533586275](https://github.com/ReallocAll/bds-test-lab/actions/runs/33533586275)
- State: **PASS**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `1417ab2acb6071b2ccd742b0655768cc4fcb65f9`
- Completed: `2026-09-01T17:16:23.968640Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/BOkqzzQkJG) | [viewer](https://spark.lucko.me/uWv3WgK168) | [viewer](https://spark.lucko.me/TNrmjVkWoz) |

- windows soak RSS: start `618467328`, end `61652992`, peak `619094016`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/ou7GeH5oNE) | [viewer](https://spark.lucko.me/PWhxkPf7UP) | [viewer](https://spark.lucko.me/IZI5iacG89) |

- linux soak RSS: start `852566016`, end `858935296`, peak `858935296`
- linux soak threads: start `27`, end `28`, peak `28`
