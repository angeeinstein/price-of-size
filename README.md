# The Price of Size

A self-hosted system that collects price, size and mass for as many purchasable
things as possible, from a gravel sack to an aircraft, and stores them cleanly
classified for statistical analysis.

- Specification: [docs/spec.md](docs/spec.md)
- Development guide and current status: [docs/development.md](docs/development.md)

```
worker nodes ──HTTPS──► coordinator (API, planner, LLM, panel) ──► PostgreSQL + pgvector
                                       └──────────────────────────► NAS (raw store, archives)
```
