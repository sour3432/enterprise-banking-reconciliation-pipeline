"""
Global Banking Reconciliation Pipeline — application entrypoint.

Runs ingestion through gold analytical marts, operational Excel reporting, and the
master executive workbook
(RAW → BRONZE → SILVER → VALIDATION → DUPLICATE INTELLIGENCE → RECONCILIATION
→ GOLD LAYER → REPORTING → MASTER EXECUTIVE WORKBOOK).
"""

from __future__ import annotations

import time
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

# Resolve package imports relative to ``src/`` when executed as a script.
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ingestion.ingestion_engine import IngestionEngine  # noqa: E402
from standardization.standardization_engine import StandardizationEngine  # noqa: E402
from validation.validation_engine import ValidationEngine  # noqa: E402
from reconciliation.duplicate_engine import DuplicateEngine  # noqa: E402
from reconciliation.reconciliation_engine import ReconciliationEngine  # noqa: E402
from reporting.gold_layer_builder import GoldLayerBuilder  # noqa: E402
from reporting.reporting_engine import ReportingEngine  # noqa: E402
from utils.logger import configure_logging, get_logger  # noqa: E402


def load_config(config_path: Path) -> Mapping[str, Any]:
    """Load YAML configuration from disk."""
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> None:
    """Execute full pipeline through gold marts and Excel reporting."""
    t0 = time.perf_counter()
    config = load_config(_ROOT / "config.yaml")

    log_level = str(config.get("log_level", "INFO"))
    log_dir = _ROOT / str(config.get("log_directory", "logs"))
    configure_logging(level=log_level, log_directory=log_dir)

    log = get_logger("main")
    log.info("Pipeline start (project root: %s)", _ROOT)

    ingest = IngestionEngine(project_root=_ROOT, config=config)
    ingest_reports = ingest.run()
    log.info(
        "Ingestion complete: %d file(s), batch %s",
        len(ingest_reports),
        ingest.processing_batch_id,
    )

    std = StandardizationEngine(project_root=_ROOT, config=config)
    std_summaries = std.run()
    log.info(
        "Standardization complete: %d file(s), batch %s",
        len(std_summaries),
        std.processing_batch_id,
    )

    val = ValidationEngine(project_root=_ROOT, config=config)
    val_summaries = val.run()
    log.info(
        "Validation complete: %d file(s), batch %s",
        len(val_summaries),
        val.processing_batch_id,
    )

    dup = DuplicateEngine(
        project_root=_ROOT,
        config=config,
        processing_batch_id=val.processing_batch_id,
    )
    dup_summary = dup.run()
    log.info(
        "Duplicate intelligence complete: %d gold file(s), %d row(s), batch %s",
        dup_summary.gold_files_processed,
        dup_summary.total_rows,
        val.processing_batch_id,
    )

    rec = ReconciliationEngine(
        project_root=_ROOT,
        config=config,
        processing_batch_id=val.processing_batch_id,
    )
    rec.run(duplicate_summary=vars(dup_summary))
    log.info(
        "Reconciliation complete (batch %s). Audit: data/audit/reconciliation_audit_log.csv",
        val.processing_batch_id,
    )

    duration_through_reconciliation = time.perf_counter() - t0
    gold = GoldLayerBuilder(
        project_root=_ROOT,
        config=config,
        processing_batch_id=val.processing_batch_id,
        pipeline_duration_seconds=duration_through_reconciliation,
    )
    mart_paths = gold.run()
    log.info(
        "Gold layer complete: transaction master at %s",
        mart_paths.get("transaction_master", ""),
    )

    duration_full = time.perf_counter() - t0
    rep = ReportingEngine(
        project_root=_ROOT,
        config=config,
        processing_batch_id=val.processing_batch_id,
    )
    report_paths = rep.run(mart_paths=mart_paths, pipeline_duration_seconds=duration_full)
    master_path = report_paths.get("master_executive")
    log.info(
        "Reporting complete: master workbook at %s",
        master_path or "outputs/excel_reports/",
    )

    log.info("Pipeline finished.")


if __name__ == "__main__":
    main()
