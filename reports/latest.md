# Latest BDS integration test

- Lab commit: `7a118fe6948de56a2f653f1088e45eb137971a3d`
- Lab Actions: [33540299728](https://github.com/ReallocAll/bds-test-lab/actions/runs/33540299728)
- State: **FAIL**
- Spark SHA: `cde587a3f0e31e7fc03bca012b2b09e86c6185ab`
- Endstone SHA: `1417ab2acb6071b2ccd742b0655768cc4fcb65f9`
- Completed: `2026-09-01T18:23:59.236053Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Windows error:** `RuntimeError: Execution profiler produced no spark viewer URL`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/WLxhArvgmF) | [viewer](https://spark.lucko.me/ymifFP09JG) | [viewer](https://spark.lucko.me/DeaE3rv7W8) |

- linux soak RSS: start `848404480`, end `854528000`, peak `854528000`
- linux soak threads: start `27`, end `28`, peak `28`
