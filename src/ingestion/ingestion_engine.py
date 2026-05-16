"""
Enterprise ingestion engine for raw banking CSV feeds.

Reads delimited files from the raw landing zone, normalizes headers,
attaches batch-level metadata, and materializes bronze extracts suitable
for downstream standardization. This module intentionally avoids
cleansing, validation, reconciliation, or analytic profiling beyond a
lightweight ingestion manifest for operations.
"""

from __future__ import annotations

import csv
import re
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from utils.logger import get_logger

_LOG = get_logger("ingestion")

# Metadata columns appended to every bronze extract (stable contract).
_METADATA_COLUMNS: tuple[str, ...] = (
    "ingestion_timestamp",
    "source_file_name",
    "processing_batch_id",
)


def _resolve_path(project_root: Path, configured: str | Path) -> Path:
    """Resolve a configured path relative to the project root when not absolute."""
    path = Path(configured)
    return path if path.is_absolute() else (project_root / path)


def _strip_column_name(name: str) -> str:
    """Remove leading and trailing whitespace, including common zero-width characters."""
    cleaned = str(name).strip()
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d"):
        cleaned = cleaned.replace(ch, "")
    return cleaned.strip()


def column_to_snake_case(name: str) -> str:
    """
    Convert a single column label to ``snake_case``.

    Non-alphanumeric runs are collapsed to a single underscore; the result
    is lowercased and never empty (falls back to ``column``).
    """
    label = _strip_column_name(name)
    label = re.sub(r"[^\w]+", "_", label, flags=re.UNICODE)
    label = re.sub(r"_+", "_", label).strip("_").lower()
    return label or "column"


def dedupe_snake_case_columns(columns: list[str]) -> dict[str, str]:
    """
    Build a rename map so that all target names are unique snake_case labels.

    Args:
        columns: Original column names from the CSV header row.

    Returns:
        Mapping from each original column name to a unique snake_case name.
    """
    rename: dict[str, str] = {}
    used: set[str] = set()
    for original in columns:
        base = column_to_snake_case(original)
        candidate = base
        suffix = 2
        while candidate in used or candidate in _METADATA_COLUMNS:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        rename[original] = candidate
    return rename


def detect_utf8_encoding(path: Path) -> str:
    """
    Detect whether the file uses UTF-8 with BOM or plain UTF-8.

    Only ``utf-8-sig`` and ``utf-8`` are attempted, per ingestion policy.
    Validation streams the file in chunks to avoid loading multi-gigabyte
    payloads into memory.

    Raises:
        UnicodeDecodeError: If the file cannot be decoded as UTF-8.
    """
    if path.stat().st_size == 0:
        return "utf-8"

    prefix = path.read_bytes()[:3]
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    chunk_size = 1_048_576
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            block.decode("utf-8")
    return "utf-8"


def discover_csv_files(raw_dir: Path) -> list[Path]:
    """
    Return sorted CSV file paths directly under ``raw_dir`` (non-recursive).

    Matching is case-insensitive on the ``.csv`` suffix.
    """
    if not raw_dir.is_dir():
        _LOG.warning("Raw directory does not exist or is not a directory: %s", raw_dir)
        return []

    files = sorted(
        p
        for p in raw_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".csv"
    )
    _LOG.info("Discovered %d CSV file(s) under %s", len(files), raw_dir)
    return files


class BadLineCounter:
    """
    Callable for ``on_bad_lines`` when the parser engine supports it.

    Counts skipped malformed rows and emits a bounded number of sample logs.
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, bad_line: list[str]) -> None:
        self.count += 1
        if self.count <= 5:
            _LOG.warning(
                "Skipping malformed row in current file (sample %d): %s",
                self.count,
                bad_line[:20],
            )
        return None


def _read_csv_chunks(
    path: Path,
    *,
    encoding: str,
    chunksize: int,
    engine: str,
    bad_line_counter: BadLineCounter | None,
) -> Iterator[pd.DataFrame]:
    """
    Yield DataFrame chunks from a CSV using chunked reads for stable memory use.

    Malformed rows are skipped. When ``engine`` is ``python`` and
    ``bad_line_counter`` is provided, skipped rows are counted for status
    reporting; the C engine accepts ``on_bad_lines='skip'`` but cannot invoke
    a custom counter (see pandas documentation).
    """
    read_kwargs: dict[str, Any] = {
        "filepath_or_buffer": path,
        "encoding": encoding,
        "chunksize": chunksize,
        "low_memory": True,
        "engine": engine,
    }

    if engine == "c":
        read_kwargs["on_bad_lines"] = "skip"
    else:
        read_kwargs["on_bad_lines"] = bad_line_counter or "skip"

    reader = pd.read_csv(**read_kwargs)

    if isinstance(reader, pd.DataFrame):
        yield reader
        return

    yield from reader


def _sanitize_bronze_stem(source: Path) -> str:
    """Derive a filesystem-safe stem for bronze outputs."""
    stem = column_to_snake_case(source.stem)
    return stem or "source"


@dataclass
class IngestionFileReport:
    """Per-source ingestion outcome row for the bronze ingestion manifest."""

    source_file_name: str
    row_count: int
    column_count: int
    ingestion_timestamp: str
    ingestion_status: str
    detected_encoding: str
    output_bronze_filename: str


@dataclass
class IngestionEngine:
    """
    Orchestrates CSV ingestion from the raw zone into bronze extracts.

    Args:
        project_root: Repository root used to resolve relative config paths.
        config: Loaded ``config.yaml`` mapping (paths, ingestion options).
        processing_batch_id: Correlates all outputs from this engine run.
        csv_chunksize: Optional override for rows per read chunk.
    """

    project_root: Path
    config: Mapping[str, Any]
    processing_batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    csv_chunksize: int | None = None

    def __post_init__(self) -> None:
        ingestion_cfg = self.config.get("ingestion") or {}
        default_chunk = int(ingestion_cfg.get("csv_chunksize", 100_000))
        self._chunksize = self.csv_chunksize or default_chunk
        self._csv_engine = str(ingestion_cfg.get("csv_engine", "python")).lower()
        if self._csv_engine not in {"c", "python"}:
            _LOG.warning(
                "Unknown ingestion.csv_engine %r; defaulting to python.",
                self._csv_engine,
            )
            self._csv_engine = "python"

        self._raw_dir = _resolve_path(
            self.project_root, self.config.get("raw_data_path", "data/raw")
        )
        self._bronze_dir = _resolve_path(
            self.project_root, self.config.get("bronze_path", "data/bronze")
        )
        out_base = _resolve_path(
            self.project_root, self.config.get("output_path", "outputs")
        )
        report_rel = ingestion_cfg.get(
            "bronze_ingestion_report_path",
            "profiling_reports/bronze_ingestion_report.csv",
        )
        self._report_path = out_base / report_rel

    def run(self) -> list[IngestionFileReport]:
        """
        Execute ingestion for every ``*.csv`` file in the configured raw directory.

        Returns:
            One :class:`IngestionFileReport` per source file (including failures).
        """
        self._bronze_dir.mkdir(parents=True, exist_ok=True)
        self._report_path.parent.mkdir(parents=True, exist_ok=True)

        sources = discover_csv_files(self._raw_dir)

        if not sources:
            _LOG.warning("No CSV sources found; writing empty ingestion report.")
            self._write_report([])
            return []

        reports: list[IngestionFileReport] = []
        for path in sources:
            reports.append(self._ingest_single_file(path))

        self._write_report(reports)
        successes = sum(1 for r in reports if r.ingestion_status == "SUCCESS")
        partial = sum(1 for r in reports if r.ingestion_status == "PARTIAL")
        _LOG.info(
            "Ingestion run %s finished: %d success, %d partial, %d failed (of %d files).",
            self.processing_batch_id,
            successes,
            partial,
            sum(1 for r in reports if r.ingestion_status == "FAILED"),
            len(reports),
        )
        return reports

    def _ingest_single_file(self, path: Path) -> IngestionFileReport:
        """Ingest one CSV into bronze and capture a manifest row."""
        started = datetime.now(timezone.utc).isoformat()
        source_name = path.name
        bad_counter = BadLineCounter()
        encoding = "utf-8"

        try:
            encoding = detect_utf8_encoding(path)
        except (OSError, UnicodeDecodeError) as exc:
            _LOG.exception("Encoding detection failed for %s", path)
            return IngestionFileReport(
                source_file_name=source_name,
                row_count=0,
                column_count=0,
                ingestion_timestamp=started,
                ingestion_status="FAILED",
                detected_encoding="unknown",
                output_bronze_filename="",
            )

        bronze_name = (
            f"{_sanitize_bronze_stem(path)}__{self.processing_batch_id}.csv"
        )
        bronze_path = self._bronze_dir / bronze_name

        row_total = 0
        column_count = 0
        header_written = False
        bad_line_counter: BadLineCounter | None = (
            bad_counter if self._csv_engine == "python" else None
        )

        try:
            rename_map: dict[str, str] | None = None
            ts_value = started

            for chunk in _read_csv_chunks(
                path,
                encoding=encoding,
                chunksize=self._chunksize,
                engine=self._csv_engine,
                bad_line_counter=bad_line_counter,
            ):
                if rename_map is None:
                    original_cols = list(chunk.columns)
                    rename_map = dedupe_snake_case_columns(original_cols)
                    column_count = len(rename_map) + len(_METADATA_COLUMNS)

                chunk = chunk.rename(columns=rename_map)
                chunk["ingestion_timestamp"] = ts_value
                chunk["source_file_name"] = source_name
                chunk["processing_batch_id"] = self.processing_batch_id

                row_total += len(chunk)
                chunk.to_csv(
                    bronze_path,
                    mode="a" if header_written else "w",
                    header=not header_written,
                    index=False,
                    quoting=csv.QUOTE_MINIMAL,
                )
                header_written = True

            if not header_written:
                _LOG.error("No readable tabular content for %s", source_name)
                return IngestionFileReport(
                    source_file_name=source_name,
                    row_count=0,
                    column_count=0,
                    ingestion_timestamp=started,
                    ingestion_status="FAILED",
                    detected_encoding=encoding,
                    output_bronze_filename=bronze_name,
                )

            skipped = bad_counter.count if self._csv_engine == "python" else 0
            if skipped:
                _LOG.warning(
                    "%d malformed row(s) skipped for %s (engine=%s)",
                    skipped,
                    source_name,
                    self._csv_engine,
                )

            if self._csv_engine == "c" and skipped == 0:
                # C engine skips silently; do not label PARTIAL without evidence.
                status = "SUCCESS"
            else:
                status = "PARTIAL" if skipped else "SUCCESS"

            return IngestionFileReport(
                source_file_name=source_name,
                row_count=row_total,
                column_count=column_count,
                ingestion_timestamp=started,
                ingestion_status=status,
                detected_encoding=encoding,
                output_bronze_filename=bronze_name,
            )

        except (OSError, pd.errors.ParserError, ValueError) as exc:
            _LOG.exception("Failed to ingest %s", path)
            if bronze_path.exists():
                try:
                    bronze_path.unlink()
                except OSError:
                    _LOG.warning("Could not remove partial bronze file %s", bronze_path)

            return IngestionFileReport(
                source_file_name=source_name,
                row_count=row_total,
                column_count=column_count,
                ingestion_timestamp=started,
                ingestion_status="FAILED",
                detected_encoding=encoding,
                output_bronze_filename=bronze_name,
            )

    def _write_report(self, rows: list[IngestionFileReport]) -> None:
        """Persist the bronze ingestion manifest CSV (required columns only)."""
        columns = [
            "source_file_name",
            "row_count",
            "column_count",
            "ingestion_timestamp",
            "ingestion_status",
            "detected_encoding",
            "output_bronze_filename",
        ]
        frame = pd.DataFrame([{c: getattr(r, c) for c in columns} for r in rows])
        if frame.empty:
            frame = pd.DataFrame(columns=columns)
        frame.to_csv(self._report_path, index=False, encoding="utf-8")
        _LOG.info("Wrote ingestion report to %s", self._report_path)
