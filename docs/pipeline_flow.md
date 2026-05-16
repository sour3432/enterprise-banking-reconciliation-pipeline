# Pipeline flow

This document outlines how data is expected to move through the system once implementation begins.

## Stage sequence

1. **Ingestion** reads configured sources and writes immutable raw captures plus typed bronze extracts.
2. **Profiling** reads bronze/silver candidates and emits profiling summaries to `outputs/profiling_reports/`.
3. **Standardization** promotes bronze to a canonical silver schema using mapping and currency rules.
4. **Validation** applies schema and business rules; failures are written to `data/rejects/` with structured reasons.
5. **Reconciliation** consumes validated silver/gold inputs and writes match results and breaks to `outputs/reconciliation_reports/` (and supporting tables/files under `data/gold/`).
6. **Audit** appends lineage and control metadata under `data/audit/`.
7. **Reporting** composes Excel packs and dashboard extracts under `outputs/`.

## Idempotency and batching

Each run should be keyed by a **batch identifier** (for example, processing date plus source sequence). Engines should be designed so reruns with the same batch id are either rejected or safely idempotent—this behavior will be specified when orchestration is added.

## Error handling (future)

Operational failures (IO, parsing) should be distinguished from **data rejects** (validation). The former surfaces to monitoring; the latter is an expected outcome with its own workflow.
