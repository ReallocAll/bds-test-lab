# BDS Test Lab Controller

Shared controller entry point for headless Bedrock Dedicated Server integration tests.

The controller is intentionally plugin-agnostic. Plugin-specific checks belong in scenarios.

Formal performance and real-BDS workflows opt into bStats evidence explicitly. The shared
integration installer writes `plugins/bstats/config.toml` as `enabled = false`, copies the
verified bytes to `bstats-config.toml`, and records the parsed value, relative path, size, and
SHA-256 in each result JSON. Workflows run `controller.verify_bstats_evidence` immediately
before upload; missing, tampered, noncanonical, or mismatched evidence blocks the upload.
