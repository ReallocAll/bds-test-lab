# Latest BDS integration test

- Lab commit: `8a7431986dbc11a43bfb43866409326ed5991e26`
- Lab Actions: [33224176747](https://github.com/ReallocAll/bds-test-lab/actions/runs/33224176747)
- State: **FAIL**
- Spark SHA: `04e9f3d6e27ba44965e2486f9bec37eb99636a8f`
- Endstone SHA: `c76c814289ee3be8a7236389b6bdeb5728b154e4`
- Completed: `2026-08-29T01:11:08.559361Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **running** | `26.44` | `controlled_crash_for_recovery` | `PASS` | `30m` | [viewer](https://spark.lucko.me/LaaEfRQENC) | [viewer](https://spark.lucko.me/x85a2CIgjf) | [viewer](https://spark.lucko.me/3GNk9GxL8b) |

- windows soak RSS: start `None`, end `None`, peak `None`
- windows soak threads: start `None`, end `None`, peak `None`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/yMRjZNZiy3) | [viewer](https://spark.lucko.me/lSuideqgtw) | [viewer](https://spark.lucko.me/YpdAyPh1It) |

- linux soak RSS: start `846413824`, end `851275776`, peak `851275776`
- linux soak threads: start `27`, end `28`, peak `28`
