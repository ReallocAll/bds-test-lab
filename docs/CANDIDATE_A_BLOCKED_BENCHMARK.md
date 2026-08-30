# Candidate A blocked benchmark

`.github/workflows/candidate-a-blocked.yml` is a manual, pre-registered benchmark for the small single-core CPU effect seen in run `33265929968`. It is intentionally separate from the existing paired and semantic workflows.

The fixed Spark commits are baseline `15b79e814ee6542f8a2382df09353e9c2009c8d1` and Candidate A `78314038b67d506ec48da9a61181c0048fb3658e`. The bot commit is `b8c4875bb1fafaa5dd9b8e91b16b613af47bf37a`. Every case uses the lab-local `scenarios/candidate-a-stationary.json` scenario (one indefinite `idle` step), SHA-256 `169360cb46acc6dc29ed5b38e082543b12434860bb65119c519a095de2a04799`, five bots, a fresh flat world with seed `8675309`, 4 ms sampling, a 60-second warmup, and an exact 600-second measurement.

The four treatments are `off-B`, `off-C`, `full-B`, and `full-C`. Blocks 1–4 use these explicit schedules, then repeat the four-row table through block 20:

```text
[off-B,  off-C,  full-B, full-C]
[off-C,  off-B,  full-C, full-B]
[full-B, full-C,  off-B,  off-C]
[full-C, full-B,  off-C,  off-B]
```

Each baseline/candidate pair is adjacent, while every treatment occupies every position once per four-block batch. One Actions matrix job runs one block (four fresh BDS cases), with `max-parallel: 1`, so each runner remains within a practical timeout. Case evidence is uploaded even when a case fails, after the generated `downloads`, `work`, and `hotspot-wheel` directories are pruned from each case root. An explicit allowlist manifest records retained file hashes and enforces a bounded evidence size; binaries, wheels, and the BDS server tree are not upload inputs. Hotspot iterations are fixed at the non-saturating value `1000`, recorded with the pre-registered rationale that the existing 1800-iteration baseline was approximately 90% off / 102% full before affinity. Process CPU, bot inputs, and per-tick MSPT are recorded against explicit warmup/measurement monotonic boundaries; packets, chunks, and input counts are balance checks only.

Spark is pinned to the baseline/candidate SHAs above and Endstone is pinned to `c76c814289ee3be8a7236389b6bdeb5728b154e4`. Each case records the Endstone repository, exact SHA, workflow run, and artifact identity; missing or drifting metadata invalidates the cumulative experiment.

The primary estimate is the unadjusted within-block difference-in-differences:

```
(full-C - full-B) - (off-C - off-B)
```

It is reported in process CPU percent of one core. CPU milliseconds per tick and MSPT are secondary metrics. Packets and chunks are balance checks only and are never regression covariates. The controller captures the latest cumulative `fleet_progress` counters available from the fixed bot at warmup start/end and measurement start/end, then records explicit warmup and measurement deltas by boundary subtraction. Stationary movement/chunk deltas may be zero; all counters must still be present, integer, and nonnegative, with no more than 50% cross-case variation. PEP monitoring callback failures, shadow overflows, and unknown code IDs must be zero. Native-boundary misses, snapshot failures, and thread mismatches are retained as diagnostics. PEP counts are full-profile cumulative and are not window-aligned.

Each managed Endstone/BDS measurement root (`python -m endstone --server-folder <exact-folder>`) is controlled with unprivileged Linux affinity at both process and `/proc/<pid>/task` TID level. The controller validates the live root PID and create-time against `ServerProcess`, records its name, executable, and command line, and rejects stale/reused PIDs. Existing root TIDs are pinned to one controlled CPU, and every one-second warmup/measurement polling interval enumerates, pins, and verifies newly observed root TIDs; descendants are not summed or used as the measurement process. The controller and bot load-generator TIDs are pinned to the disjoint remaining CPU set. Evidence records runner topology and exact TID affinities. This is controlled-process CPU isolation only: kernel scheduling and unrelated hosted-runner work are not excluded, and no privileged host mutation is required. A missing or unverifiable TID affinity fails the case.

The ordinary paired 95% Student-t interval is descriptive only. Confirmatory looks occur only after complete batches at `n=4,8,12,16,20`, using the two-sided 99% interval `mean ± t_(0.995,n-1) * s / sqrt(n)` with critical values `5.840909`, `3.499483`, `3.105807`, `2.946714`, and `2.860935`. Bonferroni gives marginal confidence `0.99` and simultaneous familywise confidence at least `0.95`. A look before block 20 stops only when its half-width is at most 0.5 percentage points: `KEEP` requires the upper bound to be below zero, `REVERT` requires the lower bound to be above zero, and otherwise the result is `INCONCLUSIVE`. At block 20, the analyzer always stops; if the half-width is above 0.5, it reports `MAX_INCONCLUSIVE` and makes no directional inference even when the interval excludes zero. Invalid evidence reports `CONTINUE` before block 20 and `MAX_INCONCLUSIVE` at block 20.

To run a later batch, dispatch with `start_block` set to 5, 9, 13, or 17 and pass the prior Actions run IDs as a comma-separated `prior_run_ids` value. The analyzer combines at most five runs in block/case order and rejects missing, duplicate, extra, SHA-drifted, or mismatched evidence.

The workflow is not dispatched by repository changes. A maintainer can validate the workflow file locally with a YAML parser and run the focused tests with:

```text
python -m unittest discover -s tests -p "test_candidate_a_blocked*.py"
git diff --check
```

The controller's upload gate can be run against an evidence directory with `python -m controller.candidate_a_blocked_benchmark --prepare-evidence --evidence-root evidence`. It removes only the three generated runtime payload directories from each case, tolerates the top-level `.candidate-a-upload-ok` control marker without uploading it, and fails closed on an unexpected file, symlink, or oversized retained file.

The exact Actions commands are prepared here but are not run by repository changes:

```text
gh workflow view candidate-a-blocked.yml --repo ReallocAll/bds-test-lab
gh workflow run candidate-a-blocked.yml --repo ReallocAll/bds-test-lab --ref develop -f start_block=1 -f batch_size=4 -f baseline_sha=15b79e814ee6542f8a2382df09353e9c2009c8d1 -f candidate_sha=78314038b67d506ec48da9a61181c0048fb3658e -f bot_ref=b8c4875bb1fafaa5dd9b8e91b16b613af47bf37a
gh run watch <run-id> --repo ReallocAll/bds-test-lab --exit-status
```

For a later batch, use `start_block=5`, `9`, `13`, or `17` and add `-f prior_run_ids=<previous-run-id>`.
