# Architecture

This document describes the **logical architecture** of the Global Banking Reconciliation Pipeline. Implementation details will evolve; the boundaries below are stable design targets.

## Design principles

- **Modular domains**: each pipeline concern lives in its own Python package under `src/`.
- **Explicit data layers**: medallion folders communicate promotion and quality expectations.
- **Audit by default**: reconciliation and validation outcomes must be explainable and persisted.
- **Thin orchestration**: `main.py` (and future schedulers) coordinate engines without embedding rules.

## Component view

```text
                 +----------------+
                 |  main / runner |
                 +--------+-------+
                          |
     +--------------------+--------------------+
     |                    |                    |
     v                    v                    v
+-----------+      +-------------+      +-------------+
| ingestion | ---> | profiling   | ---> | standardize |
+-----------+      +-------------+      +------+------+
                                               |
                                               v
                                        +-------------+
                                        | validation  |
                                        +------+------+
                                               |
                         +---------------------+---------------------+
                         |                                           |
                         v                                           v
                 +---------------+                          +-------------+
                 | reconciliation|                         | audit trail |
                 +---------------+                          +-------------+
                         |
                         v
                 +---------------+
                 | reporting     |
                 +---------------+
```

## Configuration split

- **`config.yaml`**: environment-agnostic paths, logging, and global defaults such as base currency.
- **`configs/*.yaml`**: versioned rule catalogs consumed by validation and standardization engines.
- **`configs/country_mapping.csv`**: reference data suitable for lightweight joins without a full MDM integration (placeholder rows only).

## Extension points

- Replace script-style `main.py` with a workflow engine while keeping `*Engine` public interfaces stable.
- Introduce an `interfaces/` or `contracts/` package if multiple implementations per stage are required (for example, SWIFT vs. ISO20022 ingestion).
