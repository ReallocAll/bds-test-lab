# Chunk traversal validation

`chunk-traversal-validation.yml` validates the production `bds-test-bot` against a real Bedrock Dedicated Server and the latest successful Endstone/Spark artifacts.

## Scenarios

- `chunk-fly`: 1, 5, 10, and 20 bot fleets. Every bot must receive a server flight acknowledgement and must move at least 32 blocks according to `NetworkChunkPublisherUpdate` after the initial publisher sample.
- `chunk-walk`: 20 bot comparison workload. At least half of the fleet must move at least one block according to publisher updates and at least one bot must move at least eight blocks. Terrain stalls are therefore measured rather than hidden by client prediction.

Client-side `horizontal_distance` is telemetry only and is not accepted as proof of traversal.

## Evidence

Each matrix job records:

- exact bot and test-lab revisions;
- per-bot first/last publisher coordinates and authoritative horizontal displacement;
- flight acknowledgement, AuthInput/movement/correction counters, chunks received, and chunk spans;
- a 30 second Spark profile URL;
- TPS, MSPT, process/system CPU, and BDS RSS samples;
- graceful fleet disconnect and graceful BDS shutdown evidence.

The 20-bot `chunk-fly` and `chunk-walk` jobs provide the direct traversal/load comparison. Artifacts retain the bot JSON log, BDS/Spark logs, result JSON, metadata, and server properties.
