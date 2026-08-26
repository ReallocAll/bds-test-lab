# Latest BDS integration test

- Lab commit: `c27e3ee7becab05374696f5a0f71f41ea9724030`
- Lab Actions: [32942892467](https://github.com/ReallocAll/bds-test-lab/actions/runs/32942892467)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `ad11276d1b9c0b7745acfdfefa7396169c2bcbc6`
- Completed: `2026-08-26T07:31:36.700231Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `not_started` | `not_started` | `30m` |  |  |  |
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/T25O62NRfn) | [viewer](https://spark.lucko.me/zt0Z7qp5t0) | [viewer](https://spark.lucko.me/MPZkjVhZab) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
