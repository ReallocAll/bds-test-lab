# Spark v0.6.0 Blocker #2 — Windows sampler suspension lifecycle

Status: **CLOSED**

Date: 2026-08-25

## Production fix

- Repository: `ReallocAll/spark`
- Branch: `develop`
- Spark commit: `e2d26b11636f536bf6e5ead6c4a59a20372483c4`
- Commit: `fix: restore Windows sampled threads on every exit path`
- Develop Build run: https://github.com/ReallocAll/spark/actions/runs/32826415092
- Build result: **SUCCESS**
- Windows artifact: `spark-windows-32826415092`

The fix guards every successful Windows `SuspendThread` with restoration on all return paths, makes capture cancellation explicit during sampler shutdown, publishes captured frames only after the target thread has been restored, and fails closed if restoration cannot be completed after bounded retries.

Deterministic lifecycle coverage includes context failure, stack-walk initialization failure, first-walk failure, mid-walk failure, cancellation, successful capture, resume retry, and shutdown/cancellation behavior. Windows/Linux build + CTest + clang-tidy matrices passed for the exact develop SHA.

## Real BDS + real player validation

- Test repository: `ReallocAll/bds-test-lab`
- Test workflow: `Spark Real Bot Load Test`
- Test run: https://github.com/ReallocAll/bds-test-lab/actions/runs/32827261638
- Test result: **SUCCESS**
- Windows evidence artifact: `spark-blocker2-windows-32827261638`
- Spark SHA selected by artifact provider: `e2d26b11636f536bf6e5ead6c4a59a20372483c4`
- Spark source Build run selected: `32826415092`
- Endstone SHA: `bf772c28a02db73d3119a45f58c479207254795a`
- Endstone Build run: `32770415029`
- BDS version: `26.44`

The Windows validation used an actual connected `TestBot`. BDS reported `There are 1/10 players online` and `TestBot`. The bot reached the online state after receiving 95 chunks and 294 packets.

While that real player remained online, Spark execution profiling was started asynchronously at a 4 ms interval with a 120-second timeout. `spark profiler info` then confirmed that the profiler was still active after 4 seconds with 272 samples collected.

BDS was then asked to stop **while the execution profiler was still active and the real player was connected**. The server exited gracefully in **1.194 seconds**. No residual BDS process remained. The server was subsequently restarted, Spark loaded/enabled again, `spark profiler info`, `spark tps`, `spark health`, and `spark activity` all passed, and the restarted server then shut down gracefully.

## Closure criteria

| Criterion | Result |
| --- | --- |
| Exact fix on `develop` | PASS |
| Exact develop CI green | PASS |
| Windows build + CTest | PASS |
| Windows clang-tidy | PASS |
| Deterministic failed-stage suspension restoration coverage | PASS |
| Real BDS Windows load/enable | PASS |
| Real player connected and visible to BDS | PASS |
| Execution sampler active during shutdown | PASS |
| Graceful shutdown while sampler active | PASS — 1.194 s |
| No residual BDS process | PASS |
| Restart after active-profiler shutdown | PASS |

## Conclusion

Blocker #2 is closed for the validated Spark commit `e2d26b11636f536bf6e5ead6c4a59a20372483c4`. The combination of deterministic failure-injection lifecycle tests and a real Windows BDS shutdown/restart test with an actual player connected provides evidence that Spark no longer leaves sampled threads suspended across capture failure or normal plugin/server shutdown paths.
