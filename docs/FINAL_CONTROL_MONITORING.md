# Final Spark control and monitoring comparison

`final-control-monitoring.yml` is a Linux-only paired workflow. It requires an
exact full BDS version and verifies that the installed Endstone runtime selects
the matching protocol before launch. It starts a fresh BDS+Endstone case
without the Spark plugin, then a matching case with the exact Spark artifact
deployed. Both cases use the same bounded workload, warmup, measurement
interval, and monotonic-clock evidence. CPU, MSPT/TPS, RSS, and context-switch
measurements are recorded outside the setup window.

The monitoring case proves Spark's resident services are loaded while
`spark profiler info` reports one exact inactive status before and after
measurement. During the monotonic measurement interval it snapshots Spark's
durable `plugins/spark/activity.json` and the complete server-log slice; any
activity-file change, activity entry, active status, or start/stop/session
transition fails the case. The control case records the omitted plugin path and
startup-log absence proof. Both cases write canonical disabled bStats evidence
and retain raw logs, workload metrics, provenance, and the paired
`comparison.json` artifact.
