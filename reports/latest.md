# Latest BDS integration test

- Lab commit: `1a0462003f9e0f24809fb450b774c86a04bc6d65`
- Lab Actions: [33652418080](https://github.com/ReallocAll/bds-test-lab/actions/runs/33652418080)
- State: **FAIL**
- Spark SHA: `8cb5c2b651a8c9808da397e6957526f87c1fe712`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T16:05:10.778239Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/lRysdfCtWm) | [viewer](https://spark.lucko.me/qYdtObDnWY) | [viewer](https://spark.lucko.me/YDZdRSRrZT) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
