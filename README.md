# Global Banking Reconciliation Pipeline

Enterprise-style Python foundation for multi-source banking transaction processing: ingestion, standardization, validation, reconciliation, audit, and reporting. This repository currently provides **architecture and scaffolding only**; domain logic is intentionally deferred.

## Business problem

Banking operations teams routinely reconcile transactions across core banking, payment rails, card schemes, and internal ledgers. Source formats differ, reference data drifts, and controls require explainable outcomes. This project simulates a platform that must standardize messy inputs, enforce validation, reconcile balances and references, and produce auditable evidence for regulators and internal audit.

## Architecture summary

- **Medallion-style data zones** under `data/` (`raw` → `bronze` → `silver` → `gold`) with explicit `rejects` and `audit` sinks.
- **Domain modules** under `src/`, each with a single façade class (`*Engine`) to keep orchestration readable and to allow future dependency injection.
- **Configuration as data**: runtime paths in `config.yaml`; rule and mapping catalogs in `configs/`.
- **Observability**: centralized logging via `src/utils/logger.py`, with optional file output under `logs/`.
- **Entrypoint**: `main.py` loads configuration, configures logging, and enumerates pipeline stages without executing business processing.

## Repository layout

| Path | Purpose |
|------|---------|
| `data/raw` … `data/gold` | Staged datasets aligned to medallion semantics |
| `data/rejects` | Records failing validation with reasons |
| `data/audit` | Lineage, batch metadata, and controls evidence |
| `configs/` | YAML/CSV catalogs for validation, currency, schema mapping, geography |
| `src/ingestion` | Landing and cataloging multi-source feeds |
| `src/profiling` | Quality and distribution signals |
| `src/standardization` | Canonical model and enrichment |
| `src/validation` | Rule execution and reject routing |
| `src/reconciliation` | Matching and break analysis |
| `src/audit` | Immutable audit event persistence |
| `src/reporting` | Excel and dashboard-oriented outputs |
| `src/utils` | Cross-cutting utilities (logging, future IO helpers) |
| `outputs/` | Generated reports and extracts |
| `notebooks/` | Exploratory analysis (not part of production path) |
| `tests/` | Automated tests (to be expanded) |
| `docs/` | Architecture and process documentation |

## Pipeline stages

1. **Ingestion** — discover batches, parse sources, persist to raw/bronze with batch identifiers.
2. **Profiling** — summarize completeness, cardinality, and anomalies for operational review.
3. **Standardization** — apply schema mapping, currency normalization, and reference joins.
4. **Validation** — enforce contracts and business rules; isolate rejects.
5. **Reconciliation** — match ledgers and classify breaks with explainability metadata.
6. **Audit** — record decisions, rule versions, and lineage suitable for compliance review.
7. **Reporting** — render Excel summaries, reconciliation packs, and dashboard feeds.

## Future modules (planned extensions)

- Workflow orchestration (Airflow / Dagster / cloud-native scheduler adapters).
- Secrets management integration and per-environment overlays.
- Data quality contracts as versioned artifacts (Great Expectations or custom).
- API layer for exception workflows and human approvals.

## Technologies

| Area | Stack |
|------|--------|
| Data manipulation | `pandas`, `numpy`, `pyarrow` |
| Analytics engine | `duckdb` |
| Excel reporting | `openpyxl`, `xlsxwriter` |
| Fuzzy matching | `rapidfuzz` |
| Validation | `pydantic`, `pandera` |
| Configuration | `pyyaml` |
| Dates | `python-dateutil` |

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — component view and boundaries
- [`docs/pipeline_flow.md`](docs/pipeline_flow.md) — stage sequence and data movement
- [`docs/business_rules.md`](docs/business_rules.md) — where rules will live (placeholder)

## License

Add a license file when your organization’s legal requirements are known.
