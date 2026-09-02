# Latest BDS integration test

- Lab commit: `2e0719da866c2838bfcadb876824c163dc00d5a2`
- Lab Actions: [33621841637](https://github.com/ReallocAll/bds-test-lab/actions/runs/33621841637)
- State: **PASS**
- Spark SHA: `653e6821a45f5b5de5e8d22671960c2024db18be`
- Endstone SHA: `46eff9f125f52eac76472d84339ead8fbf51fcd2`
- Completed: `2026-09-02T11:26:39.811136Z`

## Platforms

| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |
|---|---|---|---|---|---|---|---|---|
| Windows | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/24oZKZYYAa) | [viewer](https://spark.lucko.me/EutQ1Sf7h8) | [viewer](https://spark.lucko.me/if1N6ptAJ3) |

- windows soak RSS: start `617144320`, end `59076608`, peak `618819584`
- windows soak threads: start `56`, end `40`, peak `56`
| Linux | **PASS** | `26.45` | `graceful` | `PASS` | `30m` | [viewer](https://spark.lucko.me/LtyZnolEMg) | [viewer](https://spark.lucko.me/zYAqPYCpf1) | [viewer](https://spark.lucko.me/cXHRqfFRAw) |

- linux soak RSS: start `855560192`, end `863191040`, peak `863191040`
- linux soak threads: start `27`, end `28`, peak `28`
