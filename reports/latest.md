# Latest BDS integration test

- Lab commit: `2e7c98006dd91850ed7fa3106dc21387f962bc27`
- Lab Actions: [38050638465](https://github.com/ReallocAll/bds-test-lab/actions/runs/38050638465)
- State: **FAIL**
- Spark SHA: `2656b5315d17815fdabb420aa26c33d0eafea69f`
- Endstone SHA: `cce09d21a290ce804346ac2643a557ac915d3be8`
- Completed: `2026-10-10T12:05:30.586237Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **FAIL** | `` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Linux error:** `TimeoutError: Timed out after 30s waiting for Spark enable`
