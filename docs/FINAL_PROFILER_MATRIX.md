# Final Spark profiler mode matrix

`final-profiler-matrix.yml` is a Linux-only, workflow-dispatch validation for
one exact Spark commit and one exact `bds-test-bot` commit. The six matrix
cases are independent fresh BDS cases:

| Case | Console command contract |
| --- | --- |
| `default` | `spark profiler start --timeout 15` |
| `1ms` | `spark profiler start --timeout 15 --interval 1` |
| `all-thread` | `spark profiler start --timeout 15 --thread *` |
| `only-ticks-over` | `spark profiler start --timeout 15 --only-ticks-over 10` |
| `allocation` | `spark profiler start --timeout 15 --alloc` |
| `alloc-live-only` | `spark profiler start --timeout 15 --alloc-live-only` |

Each case installs the exact Spark artifact and the successful Endstone
artifact resolved by the shared provider, writes canonical disabled bStats
configuration before every BDS launch, starts the real bot workload, and
records the warmup and measurement boundaries. The controller fetches the raw
protobuf behind the direct viewer URL, stores it as a case-local
`.sparkprofile`, records its SHA-256 and size, and validates the exported mode,
interval, thread selection, tick metadata, allocation/live-only metadata, and
mode-specific diagnostics. `evidence-manifest.json` hashes the prepared case
files alongside the result's artifact and payload provenance.

The validator requires non-empty roots and samples, positive capacities,
non-negative counters, and clear exported incomplete/storage flags. Drop
counters are reported as evidence and are not required to be zero. The
`alloc-live-only` case also requires positive accepted samples, sampled bytes,
tracked live allocations/bytes, retained ages, and a fixture state proving that
the allocations were retained through export and cleaned up on shutdown. A
missing, empty, escaped, or hash-mismatched payload fails closed. The workflow
uploads prepared logs, manifests, raw profiles, and result JSON for every case
even when validation fails; no runtime payload is committed to the repository.

For `only-ticks-over`, the exported threshold and ticked aggregator are always
required, while an included-tick field may be omitted or explicitly zero. A
zero-inclusion profile is qualified without claiming positive tick selectivity;
the raw payload, diagnostics, duration, and other integrity gates still apply.
