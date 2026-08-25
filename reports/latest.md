# Latest BDS integration test

- Lab commit: `54ff0eb752b593e55b30d71f35d715117af07578`
- Lab Actions: [32881171769](https://github.com/ReallocAll/bds-test-lab/actions/runs/32881171769)
- State: **FAIL**
- Spark SHA: `5e383ad43008d6ce8967bcc8fdcab363d399443c`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Completed: `2026-08-25T18:31:53.444278Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `26.44` | `forced_after_failure` | `crashed_waiting_restart` | `30m` | [viewer](https://spark.lucko.me/I9MrA7QJad) |  |  |

**Windows error:** `TimeoutError: Timed out after 90s waiting for Spark crash recovery replay`
| Linux | **PASS** | `26.44` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/8UrqF95dup) | [viewer](https://spark.lucko.me/B0ztaSeThM) | [viewer](https://spark.lucko.me/A3qKCeSKED) |

- linux soak RSS: start `833708032`, end `839983104`, peak `839983104`
- linux soak threads: start `29`, end `30`, peak `30`
