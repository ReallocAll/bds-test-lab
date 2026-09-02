# Linux allocator ELF/module rescan measurement

## Scope

This measures the periodic Linux allocator `ElfImportHooks::rescan()` path directly rather than inferring its cost from whole-server MSPT noise. The production allocation sampler invokes the rescan from `tick()` approximately every 5 seconds, before the `count_only` early return, so persistent count-only sessions execute the same periodic scan.

Measurement Spark source: `7ffcec4c0d1e5327b79fabde9ac385fd825df322` (tree-equivalent caller-frame candidate later merged as `653e6821a45f5b5de5e8d22671960c2024db18be`).

Workflow run: `33619400554`

Artifact: `elf-rescan-direct-33619400554`, id `9842282589`, digest `sha256:55d2399d8e6aa35bc12ffecc8947eed4c87fd956da4a3c853af88ef2e911608d`.

The benchmark executes the real `elf_import_hooks.cpp` scan + installed patch path. Its hook requirements match production: `malloc`, `calloc`, `realloc`, and `free` are required; `reallocarray`, `aligned_alloc`, and `posix_memalign` are optional. Replacement addresses point to the current libc implementations so the hook walker/patcher is exercised without changing allocator semantics.

## Direct results

| Shape | Accounted modules | Hook targets | Mean rescan | p95 | p99 | Max | 5s amortized | Single-core equivalent |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Small runner process | 7 | 8 | 372.126 us | 387.849 us | 403.326 us | 429.293 us | 74.425 us/s | 0.007% |
| 64 dummy modules | 71 | 72 | 1,758.222 us | 1,804.232 us | 1,826.200 us | 1,846.533 us | 351.644 us/s | 0.035% |

The strict real-BDS alloc-live profile observed 20 hooked + 47 skipped = 67 accounted modules and 76 installed hook targets, so the 64-module benchmark is deliberately close to the actual module/target shape.

## Server-level interpretation

At 20 TPS there are approximately 100 ticks per 5-second rescan period. A 1.758ms rescan therefore corresponds to only about **0.0176ms/tick** if averaged across the period, which explains why whole-server MSPT A/B measurements are expected to be noise-dominated. The work is not evenly spread, however: the implementation performs it synchronously on the tick path, so roughly one tick per 5 seconds can receive an approximately **1.8ms** localized addition in a BDS-shaped module set.

This distinction matters: it is a measurable profiler tail-cost, but not a meaningful sustained CPU hotspot.

## Decision: POSTPONE

Do not add a production optimization in the current performance campaign.

Skipping rescans for count-only sessions would weaken process-wide coverage for modules loaded after profiler start. Moving scanning/patching off the tick thread, or adding a loader-generation/module-set cache, would preserve semantics only with substantially more lifecycle, synchronization, and unload/reload complexity. That complexity is disproportionate to an observed ~0.035% single-core amortized cost, especially because the overhead exists only while the Linux allocation profiler is active.

Revisit only if a future allocation-profiler tail-latency SLO requires eliminating an approximately 1.8ms periodic tick-path event, or if real deployments show materially larger module sets/churn. Until then this candidate is diminishing-returns evidence rather than a justified production patch.
