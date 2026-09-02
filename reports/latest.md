# Latest BDS integration test

- Lab commit: `7b61b4f9ab191b80a24ed9d5c4892f04b9ba5b5a`
- Lab Actions: [33649977988](https://github.com/ReallocAll/bds-test-lab/actions/runs/33649977988)
- State: **FAIL**
- Spark SHA: `6cbda71cf858eab29f59fd9243ea94f2110f5637`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T15:42:31.886227Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.45` | `controlled_crash_for_recovery` | `crashed_waiting_restart` | `30m` | [viewer](https://spark.lucko.me/zJjzTTsMtF) | [viewer](https://spark.lucko.me/8CMNAIAfyQ) |  |
| Linux | **running** | `26.45` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/3IsJUt8UVs) | [viewer](https://spark.lucko.me/7uAUKLFiE2) | [viewer](https://spark.lucko.me/yncbkLqrSE) |

- linux soak RSS: start `None`, end `None`, peak `None`
- linux soak threads: start `None`, end `None`, peak `None`
