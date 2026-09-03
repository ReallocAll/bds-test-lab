# PR #49 no-shim Windows BDS evidence contract

This auxiliary research path validates the exact no-shim Spark artifact produced by `ReallocAll/spark` PR #49 without changing the production artifact-selection path in `bds-test-lab`.

Pinned Spark evidence:

- SHA: `fa1910687a9c51b5475abd043d807910a0cdd34e`
- workflow: `Windows No-Shim Real Plugin Experiment`
- workflow run: `33777788557`
- artifact: `9902471289`
- artifact name: `spark-windows-no-shim-fa1910687a9c51b5475abd043d807910a0cdd34e`

The runner fails closed unless all of the above match and the artifact is unexpired. Before BDS starts it also requires:

- no `spark_allocation_shim.*` file anywhere in the Spark artifact;
- no `spark_allocation_shim.dll` entry in the uploaded `dumpbin /dependents` evidence;
- no `spark_allocation_shim` target in the uploaded target-graph evidence;
- no shim payload in the deployed BDS plugin directory.

After those gates, the existing exact Windows combined validation is reused unchanged for the measured workload: one BDS-provisioned world, three real behavior packs with state oracles, modified gamerules, 20 real protocol clients running the chunk-walk workload, health metadata, execution profiling, allocation profiling, clean restart/recovery, and verified graceful process-tree shutdown.

This branch and its draft PR are research evidence only and are not intended to be merged as part of Spark PR #49.
