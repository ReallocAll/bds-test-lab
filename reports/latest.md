# Latest BDS integration test

- Lab commit: `4f5e662bf24aaf83f99c827a404103a484d74aba`
- Lab Actions: [32943179626](https://github.com/ReallocAll/bds-test-lab/actions/runs/32943179626)
- State: **FAIL**
- Spark SHA: `43a72153291eba0a46389cf90d8a861aaf48678c`
- Endstone SHA: `ad11276d1b9c0b7745acfdfefa7396169c2bcbc6`
- Completed: `2026-08-26T07:49:18.488253Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `not_started` | `30m` | [viewer](https://spark.lucko.me/xqUU0h16gM) |  |  |

**Windows error:** `RuntimeError: Allocation profiler produced no spark viewer URL`
| Linux | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/3JoXShxvrZ) | [viewer](https://spark.lucko.me/fIv6dqmEtM) | [viewer](https://spark.lucko.me/PkMKiLE7uC) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
