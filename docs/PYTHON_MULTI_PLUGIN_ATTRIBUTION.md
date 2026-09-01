# Final Python multi-plugin attribution

`python-attribution-multi-plugin.yml` is a Linux-only workflow for one exact
Spark commit and one exact full BDS version. It verifies the Endstone runtime's
download target before launch, then installs two separately named Endstone Python wheels with
distinct modules, source identities, and call chains, then validates both in
one real execution profile. The validator requires non-empty profile roots,
positive Python attribution, distinct class-source mappings, source entries,
filtered observer frames, and zero callback or shadow failures.

The workflow records exact Spark and Endstone artifact provenance, requested
and observed BDS/runtime versions, canonical disabled bStats evidence, the raw
profile, and the parsed validation summary. It is dispatched with full Spark,
BDS, and optional Endstone SHAs/versions.
