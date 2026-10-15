# Latest BDS integration test

- Lab commit: `fcc690fa8243fbfaef10a25a82dc2033070e5daa`
- Lab Actions: [38672010997](https://github.com/ReallocAll/bds-test-lab/actions/runs/38672010997)
- State: **FAIL**
- Spark SHA: `2656b5315d17815fdabb420aa26c33d0eafea69f`
- Endstone SHA: `cce09d21a290ce804346ac2643a557ac915d3be8`
- Completed: `2026-10-15T02:39:30.312465Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **FAIL** | `` | `not_started` | `not_started` | `30m` |  |  |  |

**Windows error:** `FileNotFoundError: No file matching ['spark_allocation_shim.dll'] under D:\a\bds-test-lab\bds-test-lab\downloads\spark\payload`
| Linux | **FAIL** | `` | `forced_after_failure` | `not_started` | `30m` |  |  |  |

**Linux error:** `TimeoutError: Timed out after 30s waiting for Spark enable`
