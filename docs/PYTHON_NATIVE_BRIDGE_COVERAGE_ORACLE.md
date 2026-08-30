# Python native bridge coverage oracle

The `python-native-bridge-coverage-oracle` workflow is a correctness check for
Spark's Python attribution bridge on Linux CPython 3.13. It is not a CPU,
MSPT, TPS, or performance benchmark.

Each run waits for successful Spark `Build` and `Python attribution` workflows
for parent `78314038b67d506ec48da9a61181c0048fb3658e` and candidate
`ea0af5f3abf1817bba126b3cc9bfe78d837cb329`. It then starts ten fresh BDS
processes in five interleaved parent/candidate pairs. Every process warms the
fixture for 30 seconds and profiles for 60 seconds at the unchanged 4 ms
execution-sampler interval.

`CoveragePlugin.fixed_window_tick` runs on the main-thread scheduler and keeps
its Python frame active for a 20 ms `perf_counter_ns` wall-time window while a
nested pure-Python function generates PEP 669 events. The fixture exports
monotonic timestamped window records and cumulative counters without per-call
I/O. The controller requires observable Spark start, stop-request, and
stop-complete acknowledgements. It first uses the acknowledged command
interval to select complete fixture records, then makes the first selected
record start and last selected record end the effective profile/workload
interval. Thus the workload is not reported as a strict subset of the
interval being compared. Both the cumulative evidence and this derived
`coverage-counters.json` are retained.

The analyzer reconstructs `coverage-counters.json` from the cumulative records
and the recorded effective boundaries and exact-compares the resulting JSON
object. A stale, edited, reordered, or otherwise inconsistent aligned file is
therefore rejected. It also fails closed when the clock, acknowledgements,
boundaries, ordering, or complete-record proof is missing.

The analyzer reparses every raw profile, requires the known fixed-window to
nested-call chain, validates the Python diagnostics and aligned workload
counters, and rejects observer callback leakage. It computes three paired
candidate-minus-parent endpoints:

1. global attributed-sample fraction, margin -1.0 percentage points;
2. outer `fixed_window_tick` inclusive weight / main-thread weight, margin
   -2.0 percentage points;
3. nested `nested_call` inclusive weight / outer `fixed_window_tick` inclusive
   weight, margin -2.0 percentage points.

Each endpoint reports the deltas in percentage points and requires the
two-sided 95% paired-t lower bound to be strictly above its margin. Zero
paired variance is reported as a finite degenerate interval `[mean, mean]`;
the margin comparison still determines PASS or FAIL. Missing data or an
interval that does not clear a margin cannot produce PASS. Raw cumulative
counters, aligned counters, profile boundaries, profiles, and logs remain
uploaded on every case outcome. The raw Spark profile's metadata interval is
retained separately; the registered correctness interval is the acknowledged
session's complete-record interval described above.

Manual dispatch:

```sh
gh workflow run python-native-bridge-coverage-oracle.yml --repo ReallocAll/bds-test-lab
```
