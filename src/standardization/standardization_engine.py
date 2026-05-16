"""
Silver-layer standardization engine.

Transforms bronze CSV extracts into deterministic, auditable silver datasets
with preserved raw values, derived canonical fields, and lineage metadata.
This module intentionally excludes validation, reconciliation, and reporting
beyond the operational summaries required for standardization governance.
"""

from __future__ import annotations

import csv
import math
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yaml

from utils.logger import get_logger

_LOG = get_logger("standardization")

_INVISIBLE_CHARS = dict.fromkeys(
    map(
        ord,
        (
            "\u200b",
            "\u200c",
            "\u200d",
            "\ufeff",
            "\u00a0",
            "\u202f",
            "\u2060",
        ),
    ),
    None,
)

_NULL_STRINGS = frozenset(
    {
        "",
        "null",
        "none",
        "nan",
        "n/a",
        "na",
        "-",
        "--",
        "nil",
        "end_of_file",
    }
)

_ISO_CURRENCY_CODES = frozenset(
    {
        "AED",
        "AUD",
        "BHD",
        "CAD",
        "CHF",
        "CNY",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "INR",
        "JPY",
        "KWD",
        "MYR",
        "NOK",
        "NZD",
        "OMR",
        "QAR",
        "SAR",
        "SEK",
        "SGD",
        "USD",
        "ZAR",
        "TRY",
        "THB",
        "PLN",
        "PHP",
        "PKR",
        "BDT",
        "LKR",
        "IDR",
        "KRW",
        "MXN",
        "BRL",
        "RUB",
        "ILS",
        "CLP",
        "COP",
        "EGP",
        "NGN",
        "KES",
        "UGX",
        "TZS",
        "MAD",
        "DZD",
        "TND",
        "JOD",
        "LBP",
        "IQD",
    }
)

_CURRENCY_SYMBOL_MAP = {
    "₹": "INR",
    "€": "EUR",
    "£": "GBP",
    "$": "USD",
    "¥": "JPY",
    "د.إ": "AED",
    "aed": "AED",
    "usd": "USD",
    "eur": "EUR",
    "inr": "INR",
    "gbp": "GBP",
    "jpy": "JPY",
}


def _resolve_path(project_root: Path, configured: str | Path) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (project_root / path)


def _strip_invisible(value: str) -> str:
    return value.translate(_INVISIBLE_CHARS).strip()


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def discover_bronze_csvs(bronze_dir: Path) -> list[Path]:
    """Return sorted bronze CSV paths (non-recursive)."""
    if not bronze_dir.is_dir():
        _LOG.warning("Bronze directory missing or not a directory: %s", bronze_dir)
        return []
    files = sorted(p for p in bronze_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    _LOG.info("Discovered %d bronze CSV file(s) under %s", len(files), bronze_dir)
    return files


def normalize_header_label(name: str) -> str:
    """Trim hidden whitespace and coerce to snake_case for column alignment."""
    label = _strip_invisible(str(name))
    label = re.sub(r"[^\w]+", "_", label, flags=re.UNICODE)
    label = re.sub(r"_+", "_", label).strip("_").lower()
    return label or "column"


def apply_column_synonyms(
    columns: list[str],
    synonyms: Mapping[str, str],
) -> tuple[dict[str, str], int]:
    """
    Build rename map from synonym definitions.

    Returns:
        rename_map, number_of_renames_applied
    """
    rename: dict[str, str] = {}
    used_targets: set[str] = set(columns)
    count = 0
    for src, tgt in synonyms.items():
        if src not in columns:
            continue
        target = normalize_header_label(tgt)
        candidate = target
        suffix = 2
        while candidate in used_targets and candidate != src:
            candidate = f"{target}_{suffix}"
            suffix += 1
        if src != candidate:
            rename[src] = candidate
            used_targets.add(candidate)
            count += 1
    return rename, count


def standardize_column_names(
    df: pd.DataFrame,
    synonyms: Mapping[str, str],
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, str]]:
    """
    Trim invisible characters, snake_case headers, and apply synonym mapping.

    Returns:
        Updated dataframe, audit rows for column mapping events, and a mapping
        from original bronze headers to final silver column names.
    """
    audits: list[dict[str, Any]] = []
    originals = list(df.columns)
    normalized = {col: normalize_header_label(col) for col in df.columns}
    interim = df.rename(columns=normalized)

    rename_map, _ = apply_column_synonyms(list(interim.columns), synonyms)
    if rename_map:
        for old, new in rename_map.items():
            audits.append(
                {
                    "column_name": new,
                    "original_value": old,
                    "standardized_value": new,
                    "transformation_rule": "column_synonym_map",
                }
            )
        interim = interim.rename(columns=rename_map)

    final_labels = {col: normalize_header_label(col) for col in interim.columns}
    interim = interim.rename(columns=final_labels)
    column_map = dict(zip(originals, list(interim.columns)))
    return interim, audits, column_map


def sanitize_string_columns(
    df: pd.DataFrame,
    *,
    exclude: set[str],
    uppercase: set[str],
) -> int:
    """Apply deterministic string sanitation in-place; returns count of changed cells."""
    actions = 0
    invisible_pat = r"[\u200b\u200c\u200d\ufeff\u00a0\u202f\u2060]"

    for col in df.columns:
        if col in exclude or col.endswith("_raw"):
            continue
        if df[col].dtype != object and not pd.api.types.is_string_dtype(df[col]):
            continue

        original = df[col].astype("string")
        series = original.str.replace(invisible_pat, "", regex=True)
        for old, new in (
            ("\u2018", "'"),
            ("\u2019", "'"),
            ("\u201c", '"'),
            ("\u201d", '"'),
        ):
            series = series.str.replace(old, new, regex=False)

        series = series.str.replace(r"\s+", " ", regex=True).str.strip()
        lowered = series.str.lower()
        series = series.mask(lowered.isin(_NULL_STRINGS), pd.NA)

        if col in uppercase:
            series = series.str.upper()

        sentinel = "\x00\x00SILVER_NULL\x00\x00"
        actions += int(series.fillna(sentinel).ne(original.fillna(sentinel)).sum())
        df[col] = series
    return actions


def _extract_iso_currency(text: str) -> Optional[str]:
    match = re.search(r"\b([A-Z]{3})\b", text.upper())
    if match and match.group(1) in _ISO_CURRENCY_CODES:
        return match.group(1)
    upper = text.upper()
    for sym, code in _CURRENCY_SYMBOL_MAP.items():
        if sym in text or sym.upper() in upper:
            return code
    if "RS" in upper or "RS." in upper or "INR" in upper or "₹" in text:
        return "INR"
    if "AED" in upper:
        return "AED"
    if "USD" in upper:
        return "USD"
    if "EUR" in upper:
        return "EUR"
    if "GBP" in upper:
        return "GBP"
    if "JPY" in upper:
        return "JPY"
    return None


def _parse_numeric_core(amount_text: str) -> Optional[float]:
    """Parse numeric portion after currency tokens are stripped."""
    s = amount_text.strip()
    if not s:
        return None

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        neg = True
        s = s[1:].strip()

    s = re.sub(r"[^\d,.]", "", s)
    if not s:
        return None

    last_comma = s.rfind(",")
    last_dot = s.rfind(".")

    if last_comma != -1 and last_dot != -1:
        if last_comma > last_dot:
            # European: thousands '.', decimal ','
            s_norm = s.replace(".", "").replace(",", ".")
        else:
            # US / INR grouping: thousands ',', decimal '.'
            s_norm = s.replace(",", "")
    elif last_comma != -1 and last_dot == -1:
        parts = s.split(",")
        if len(parts[-1]) == 2 and len(parts) > 1:
            s_norm = ",".join(parts[:-1]).replace(",", "") + "." + parts[-1]
        else:
            s_norm = s.replace(",", "")
    elif last_dot != -1 and last_comma == -1:
        parts = s.split(".")
        if len(parts[-1]) == 2 and len(parts) > 1 and any(len(p) == 3 for p in parts[1:-1]):
            s_norm = "".join(parts[:-1]) + "." + parts[-1]
        else:
            s_norm = s.replace(",", "")
    else:
        s_norm = s

    try:
        value = float(s_norm)
    except ValueError:
        return None
    return -value if neg else value


def parse_amount_currency(raw: Any) -> tuple[Optional[float], Optional[str], bool]:
    """
    Extract numeric amount and ISO currency from messy banking strings.

    Returns:
        (amount, currency_code, parse_failed) where ``parse_failed`` is True
        when the input is non-empty but cannot be interpreted reliably.
    """
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return None, None, False

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None, None, True
        if abs(val) > 20000 and abs(val) < 80000:
            return val, None, False
        return val, None, False

    text = _strip_invisible(str(raw))
    if text == "" or text.lower() in _NULL_STRINGS:
        return None, None, False

    currency = _extract_iso_currency(text)
    working = text
    working = re.sub(r"\b[A-Z]{3}\b", " ", working, flags=re.IGNORECASE)
    for token in ("Rs.", "Rs", "INR", "USD", "EUR", "AED", "GBP", "JPY"):
        working = re.sub(rf"\b{token}\b", " ", working, flags=re.IGNORECASE)
    working = re.sub(r"[₹$€£]", " ", working)

    numeric = _parse_numeric_core(working)
    if numeric is None:
        stripped_digits = re.sub(r"[^\d]", "", working)
        if stripped_digits == "":
            return None, None, False
        return None, currency, True
    return numeric, currency, False


def _identifier_coarse(value: Any) -> str:
    """Loose alphanumeric projection for change detection."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = _strip_invisible(str(value)).strip()
    if text.lower() in _NULL_STRINGS:
        return ""
    return re.sub(r"[^0-9A-Z]", "", text.upper())


def standardize_identifier(value: Any) -> str:
    """Remove decorative punctuation while keeping business-safe identifiers."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""

    text = _strip_invisible(str(value)).strip()
    if text.lower() in _NULL_STRINGS:
        return ""

    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]

    text = text.upper()
    text = re.sub(r"[\s\-_/]+", "", text)
    text = re.sub(r"[^0-9A-Z]", "", text)
    return text


def apply_enum_normalization(value: Any, mapping: Mapping[str, str]) -> Any:
    """Apply explicit enum synonyms then canonical casing rules."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return pd.NA

    text = _strip_invisible(str(value)).strip()
    if text == "" or text.lower() in _NULL_STRINGS:
        return pd.NA

    key = text.lower()
    if key in mapping:
        return mapping[key]
    collapsed = re.sub(r"\s+", " ", text).strip().upper()
    return collapsed


def _audit_row(
    source_file: str,
    column_name: str,
    original_value: Any,
    standardized_value: Any,
    rule: str,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "source_file": source_file,
        "column_name": column_name,
        "original_value": original_value,
        "standardized_value": standardized_value,
        "transformation_rule": rule,
        "transformation_timestamp": timestamp,
    }


class AuditBuffer:
    """Append-only audit writer with batched flushing."""

    def __init__(self, path: Path, *, flush_every: int = 5000) -> None:
        self.path = path
        self.flush_every = flush_every
        self.rows: list[dict[str, Any]] = []
        self._header_written = path.exists() and path.stat().st_size > 0

    def extend(self, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)
        if len(self.rows) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        frame = pd.DataFrame(self.rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(
            self.path,
            mode="a",
            header=not self._header_written,
            index=False,
            encoding="utf-8",
        )
        self._header_written = True
        self.rows.clear()


@dataclass
class StandardizationSummary:
    bronze_file_name: str
    rows_processed: int
    invalid_dates: int
    currency_parse_failures: int
    columns_standardized: int
    normalization_actions_count: int
    processing_duration_seconds: float
    standardization_batch_id: str


@dataclass
class StandardizationEngine:
    """
    Deterministic bronze-to-silver standardization orchestrator.

    Args:
        project_root: Repository root for resolving relative paths.
        config: Parsed ``config.yaml`` contents.
        processing_batch_id: Correlates silver outputs and audit rows.
    """

    project_root: Path
    config: Mapping[str, Any]
    processing_batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        std_cfg = self.config.get("standardization") or {}
        self._chunksize = int(std_cfg.get("csv_chunksize", 100_000))
        schema_rel = std_cfg.get("schema_mapping_path", "configs/schema_mapping.yaml")
        self._schema_path = _resolve_path(self.project_root, schema_rel)
        self._bronze_dir = _resolve_path(
            self.project_root, self.config.get("bronze_path", "data/bronze")
        )
        self._silver_dir = _resolve_path(
            self.project_root, self.config.get("silver_path", "data/silver")
        )
        audit_dir = _resolve_path(self.project_root, self.config.get("audit_path", "data/audit"))
        audit_name = std_cfg.get("audit_log_filename", "standardization_audit_log.csv")
        self._audit_path = audit_dir / audit_name

        out_base = _resolve_path(self.project_root, self.config.get("output_path", "outputs"))
        summary_rel = std_cfg.get(
            "summary_report_path", "profiling_reports/standardization_summary.csv"
        )
        self._summary_path = out_base / summary_rel

        self._schema = _load_yaml(self._schema_path)
        self._synonyms: Mapping[str, str] = self._schema.get("column_synonyms") or {}
        self._date_columns: list[str] = list(self._schema.get("date_columns") or [])
        self._amount_columns: list[str] = list(self._schema.get("amount_columns") or [])
        self._identifier_columns: list[str] = list(self._schema.get("identifier_columns") or [])
        self._enum_columns: list[str] = list(self._schema.get("enum_columns") or [])
        enum_maps = self._schema.get("enum_maps") or {}
        self._enum_maps_lower: dict[str, dict[str, str]] = {
            col: {str(k).lower(): str(v) for k, v in mapping.items()}
            for col, mapping in enum_maps.items()
        }
        self._uppercase_cols = set(self._schema.get("string_uppercase_columns") or [])
        self._string_exclude = set(self._schema.get("string_sanitize_exclude_columns") or [])

    def run(self) -> list[StandardizationSummary]:
        """Execute bronze-to-silver standardization for all discovered CSV feeds."""
        self._silver_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._summary_path.parent.mkdir(parents=True, exist_ok=True)

        if self._audit_path.exists():
            self._audit_path.unlink()

        sources = discover_bronze_csvs(self._bronze_dir)
        if not sources:
            _LOG.warning("No bronze CSV files found; writing empty summary.")
            pd.DataFrame(
                columns=[
                    "bronze_file_name",
                    "rows_processed",
                    "invalid_dates",
                    "currency_parse_failures",
                    "columns_standardized",
                    "normalization_actions_count",
                    "processing_duration_seconds",
                    "standardization_batch_id",
                ]
            ).to_csv(self._summary_path, index=False, encoding="utf-8")
            return []

        summaries: list[StandardizationSummary] = []
        for path in sources:
            summaries.append(self._standardize_file(path))
        self._write_summary(summaries)
        return summaries

    def _write_summary(self, rows: list[StandardizationSummary]) -> None:
        frame = pd.DataFrame([vars(r) for r in rows])
        frame.to_csv(self._summary_path, index=False, encoding="utf-8")
        _LOG.info("Wrote standardization summary to %s", self._summary_path)

    def _read_csv_chunks(self, path: Path) -> Iterator[pd.DataFrame]:
        read_kwargs: dict[str, Any] = {
            "filepath_or_buffer": path,
            "chunksize": self._chunksize,
            "encoding": "utf-8",
            "encoding_errors": "replace",
            "on_bad_lines": "skip",
            "dtype": str,
            "keep_default_na": False,
        }
        try:
            reader = pd.read_csv(**read_kwargs)
        except TypeError:
            read_kwargs.pop("encoding_errors", None)
            reader = pd.read_csv(**read_kwargs)
        if isinstance(reader, pd.DataFrame):
            yield reader
            return
        yield from reader

    def _standardize_file(self, path: Path) -> StandardizationSummary:
        start = time.perf_counter()
        source_name = path.name
        audit_ts = datetime.now(timezone.utc).isoformat()
        audit_buffer = AuditBuffer(self._audit_path)
        audit_events_remaining = [200_000]

        silver_name = f"{normalize_header_label(path.stem)}__{self.processing_batch_id}.csv"
        silver_path = self._silver_dir / silver_name

        rows_processed = 0
        invalid_dates = 0
        currency_failures = 0
        normalization_actions = 0
        columns_touched: set[str] = set()
        column_map: dict[str, str] = {}

        header_written = False

        try:
            try:
                header_df = pd.read_csv(
                    path,
                    nrows=0,
                    encoding="utf-8",
                    encoding_errors="replace",
                    dtype=str,
                    keep_default_na=False,
                )
            except TypeError:
                header_df = pd.read_csv(
                    path, nrows=0, encoding="utf-8", dtype=str, keep_default_na=False
                )

            _, rename_audit, column_map = standardize_column_names(header_df, self._synonyms)
            columns_touched.update(column_map.values())

            for row in rename_audit:
                audit_buffer.extend(
                    [
                        _audit_row(
                            source_name,
                            row["column_name"],
                            row["original_value"],
                            row["standardized_value"],
                            row["transformation_rule"],
                            audit_ts,
                        )
                    ]
                )

            for chunk in self._read_csv_chunks(path):
                chunk.rename(columns=column_map, inplace=True)

                self._ensure_raw_snapshots(chunk, columns_touched)

                if "processing_batch_id" in chunk.columns and "ingestion_processing_batch_id" not in chunk.columns:
                    chunk["ingestion_processing_batch_id"] = chunk["processing_batch_id"]

                normalization_actions += sanitize_string_columns(
                    chunk,
                    exclude=self._string_exclude,
                    uppercase=self._uppercase_cols,
                )

                invalid_dates += self._apply_date_columns(
                    chunk, source_name, audit_ts, audit_buffer
                )
                currency_failures += self._apply_amount_columns(
                    chunk, source_name, audit_ts, audit_buffer
                )
                normalization_actions += self._apply_identifier_columns(
                    chunk,
                    source_name,
                    audit_ts,
                    audit_buffer,
                    audit_events_remaining,
                )
                normalization_actions += self._apply_enum_columns(
                    chunk,
                    source_name,
                    audit_ts,
                    audit_buffer,
                    audit_events_remaining,
                )

                self._apply_lineage_metadata(chunk, bronze_file_name=source_name)

                chunk.to_csv(
                    silver_path,
                    mode="a" if header_written else "w",
                    header=not header_written,
                    index=False,
                    quoting=csv.QUOTE_MINIMAL,
                )
                header_written = True
                rows_processed += len(chunk)

            audit_buffer.flush()

            duration = time.perf_counter() - start
            derived_columns = (
                len(self._date_columns) * 3
                + len(self._amount_columns) * 3
                + len(self._identifier_columns)
                + len(self._enum_columns)
                + 6
            )
            columns_standardized = len(column_map) + int(derived_columns)

            summary = StandardizationSummary(
                bronze_file_name=source_name,
                rows_processed=rows_processed,
                invalid_dates=invalid_dates,
                currency_parse_failures=currency_failures,
                columns_standardized=columns_standardized,
                normalization_actions_count=normalization_actions,
                processing_duration_seconds=round(duration, 4),
                standardization_batch_id=self.processing_batch_id,
            )
            _LOG.info(
                "Standardized %s -> %s (%d rows, %.2fs)",
                source_name,
                silver_name,
                rows_processed,
                duration,
            )
            return summary

        except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError):
            audit_buffer.flush()
            _LOG.exception("Standardization failed for %s", path)
            if silver_path.exists():
                try:
                    silver_path.unlink()
                except OSError:
                    _LOG.warning("Unable to remove partial silver file %s", silver_path)
            duration = time.perf_counter() - start
            return StandardizationSummary(
                bronze_file_name=source_name,
                rows_processed=rows_processed,
                invalid_dates=invalid_dates,
                currency_parse_failures=currency_failures,
                columns_standardized=len(column_map),
                normalization_actions_count=normalization_actions,
                processing_duration_seconds=round(duration, 4),
                standardization_batch_id=self.processing_batch_id,
            )

    def _ensure_raw_snapshots(self, chunk: pd.DataFrame, columns_touched: set[str]) -> None:
        """Capture immutable raw snapshots prior to in-place string sanitation."""
        for col in self._date_columns + self._amount_columns:
            if col not in chunk.columns:
                continue
            raw_col = f"{col}_raw"
            if raw_col not in chunk.columns:
                chunk[raw_col] = chunk[col].copy()
                columns_touched.add(raw_col)

    def _apply_date_columns(
        self,
        chunk: pd.DataFrame,
        source_name: str,
        audit_ts: str,
        audit_buffer: AuditBuffer,
    ) -> int:
        failures = 0
        for col in self._date_columns:
            if col not in chunk.columns:
                continue

            raw_series = chunk[col].astype("string")
            lowered = raw_series.str.lower()
            empty = raw_series.isna() | raw_series.str.strip().eq("") | lowered.isin(_NULL_STRINGS)

            try:
                parsed = pd.to_datetime(
                    raw_series,
                    errors="coerce",
                    utc=True,
                    dayfirst=True,
                    format="mixed",
                )
            except (TypeError, ValueError):
                parsed = pd.to_datetime(
                    raw_series, errors="coerce", utc=True, dayfirst=True
                )

            excel_numeric = pd.to_numeric(raw_series, errors="coerce")
            excel_mask = excel_numeric.between(20000, 80000) & parsed.isna()
            excel_parsed = pd.to_datetime(
                excel_numeric.where(excel_mask),
                unit="D",
                origin="1899-12-30",
                utc=True,
            )
            parsed = parsed.combine_first(excel_parsed)

            unresolved = parsed.isna() & ~empty
            failures += int(unresolved.sum())

            if unresolved.any():
                sample = raw_series[unresolved].head(50_000)
                audit_buffer.extend(
                    [
                        _audit_row(
                            source_name,
                            col,
                            raw_value,
                            "",
                            "date_parse_failed",
                            audit_ts,
                        )
                        for raw_value in sample
                    ]
                )

            utc_parsed = pd.to_datetime(parsed, utc=True, errors="coerce")
            formatted = utc_parsed.dt.strftime("%Y-%m-%d %H:%M:%S")
            chunk[f"{col}_parsed"] = np.where(parsed.notna(), formatted, pd.NA)
            chunk[f"{col}_parse_confidence"] = np.where(parsed.notna(), 0.92, 0.0)
            chunk[f"{col}_parse_failed"] = unresolved.to_numpy()
        return failures

    def _apply_amount_columns(
        self,
        chunk: pd.DataFrame,
        source_name: str,
        audit_ts: str,
        audit_buffer: AuditBuffer,
    ) -> int:
        failures = 0
        for col in self._amount_columns:
            if col not in chunk.columns:
                continue
            amt_col = f"{col}_amount_numeric"
            cur_col = f"{col}_currency_code"
            fail_col = f"{col}_amount_parse_failed"

            mapped = chunk[col].map(parse_amount_currency)
            fail_mask = mapped.map(lambda t: t[2])
            failures += int(fail_mask.sum())

            if fail_mask.any():
                failed_rows = chunk.loc[fail_mask, col].head(50_000)
                audit_buffer.extend(
                    [
                        _audit_row(
                            source_name,
                            col,
                            raw,
                            "",
                            "amount_parse_failed",
                            audit_ts,
                        )
                        for raw in failed_rows
                    ]
                )

            chunk[amt_col] = mapped.map(lambda t: t[0])
            chunk[cur_col] = mapped.map(lambda t: t[1])
            chunk[fail_col] = fail_mask.to_numpy()
        return failures

    def _apply_identifier_columns(
        self,
        chunk: pd.DataFrame,
        source_name: str,
        audit_ts: str,
        audit_buffer: AuditBuffer,
        audit_events_remaining: list[int],
    ) -> int:
        actions = 0
        for col in self._identifier_columns:
            if col not in chunk.columns:
                continue
            target = f"{col}_standardized"
            standardized = chunk[col].map(standardize_identifier)
            coarse = chunk[col].map(_identifier_coarse)
            changed = coarse.ne("") & standardized.ne(coarse)
            actions += int(changed.sum())
            chunk[target] = standardized

            if audit_events_remaining[0] <= 0 or not changed.any():
                continue

            n = min(int(changed.sum()), audit_events_remaining[0])
            raws = chunk.loc[changed, col].head(n)
            stds = standardized.loc[changed].head(n)
            audit_rows = [
                _audit_row(
                    source_name,
                    col,
                    raw_val,
                    std_val,
                    "identifier_normalization",
                    audit_ts,
                )
                for raw_val, std_val in zip(raws, stds, strict=False)
            ]
            audit_buffer.extend(audit_rows)
            audit_events_remaining[0] -= len(audit_rows)
        return actions

    def _apply_enum_columns(
        self,
        chunk: pd.DataFrame,
        source_name: str,
        audit_ts: str,
        audit_buffer: AuditBuffer,
        audit_events_remaining: list[int],
    ) -> int:
        actions = 0
        for col in self._enum_columns:
            if col not in chunk.columns:
                continue
            mapping = self._enum_maps_lower.get(col, {})
            before = chunk[col]
            after = before.map(lambda value: apply_enum_normalization(value, mapping))
            same = before.eq(after) | (pd.isna(before) & pd.isna(after))
            changed = ~same
            actions += int(changed.sum())
            chunk[col] = after

            if audit_events_remaining[0] <= 0 or not changed.any():
                continue

            n = min(int(changed.sum()), audit_events_remaining[0])
            before_vals = before.loc[changed].head(n)
            after_vals = after.loc[changed].head(n)
            audit_rows = [
                _audit_row(
                    source_name,
                    col,
                    b_val,
                    a_val,
                    "enum_normalization",
                    audit_ts,
                )
                for b_val, a_val in zip(before_vals, after_vals, strict=False)
            ]
            audit_buffer.extend(audit_rows)
            audit_events_remaining[0] -= len(audit_rows)
        return actions

    def _apply_lineage_metadata(self, chunk: pd.DataFrame, *, bronze_file_name: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        chunk["standardization_timestamp"] = ts
        chunk["pipeline_stage"] = "silver"
        chunk["source_layer"] = "bronze"
        chunk["source_file_name"] = bronze_file_name
        chunk["processing_batch_id"] = self.processing_batch_id
