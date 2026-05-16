"""
Silver-to-gold validation engine for banking reconciliation pipelines.

Evaluates standardized silver datasets with severity-graded rules, routes rows
to gold / warnings / rejects, and emits audit and profiling summaries without
mutating source silver extracts.
"""

from __future__ import annotations

import csv
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yaml

from utils.logger import get_logger

_LOG = get_logger("validation")

_SEVERITY_ORDER = {"S0": 0, "S1": 1, "S2": 2, "S3": 3, "S4": 4, "S5": 5}


def _resolve_path(project_root: Path, configured: str | Path) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (project_root / path)


def _sev_rank(code: str) -> int:
    return _SEVERITY_ORDER.get(str(code).upper(), 0)


def _rank_to_code(rank: int) -> str:
    return f"S{int(rank)}" if 0 <= rank <= 5 else "S0"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def discover_silver_csvs(silver_dir: Path) -> list[Path]:
    if not silver_dir.is_dir():
        _LOG.warning("Silver directory missing or not a directory: %s", silver_dir)
        return []
    files = sorted(p for p in silver_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    _LOG.info("Discovered %d silver CSV file(s) under %s", len(files), silver_dir)
    return files


def _normalize_stem(path: Path) -> str:
    stem = re.sub(r"[^\w]+", "_", path.stem, flags=re.UNICODE)
    stem = re.sub(r"_+", "_", stem).strip("_").lower()
    return stem or "silver"


@dataclass
class ValidationFileSummary:
    source_file: str
    rows_processed: int
    rows_rejected: int
    rows_warning: int
    rows_valid: int
    warning_counts: int
    severity_distribution: str
    duplicate_transaction_count: int
    validation_duration_seconds: float
    validation_batch_id: str


@dataclass
class ValidationEngine:
    """
    Orchestrates silver validation, classification, and operational outputs.

    Args:
        project_root: Repository root for resolving relative paths.
        config: Parsed ``config.yaml`` contents.
        processing_batch_id: Correlates validation outputs for this run.
    """

    project_root: Path
    config: Mapping[str, Any]
    processing_batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        val_cfg = self.config.get("validation") or {}
        self._rules_path = _resolve_path(
            self.project_root,
            val_cfg.get("validation_rules_path", "configs/validation_rules.yaml"),
        )
        self._silver_dir = _resolve_path(
            self.project_root, self.config.get("silver_path", "data/silver")
        )
        self._gold_dir = _resolve_path(
            self.project_root, self.config.get("gold_path", "data/gold")
        )
        self._rejects_dir = _resolve_path(
            self.project_root, self.config.get("rejects_path", "data/rejects")
        )
        self._warnings_dir = self._rejects_dir / "warnings"
        audit_dir = _resolve_path(self.project_root, self.config.get("audit_path", "data/audit"))
        self._audit_path = audit_dir / val_cfg.get(
            "validation_audit_log_filename", "validation_audit_log.csv"
        )
        out_base = _resolve_path(self.project_root, self.config.get("output_path", "outputs"))
        self._summary_path = out_base / val_cfg.get(
            "validation_summary_path", "profiling_reports/validation_summary.csv"
        )
        self._rules: dict[str, Any] = _load_yaml(self._rules_path)

    def run(self) -> list[ValidationFileSummary]:
        """Validate all silver CSV feeds and emit gold, rejects, warnings, audit, summary."""
        self._gold_dir.mkdir(parents=True, exist_ok=True)
        self._rejects_dir.mkdir(parents=True, exist_ok=True)
        self._warnings_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._summary_path.parent.mkdir(parents=True, exist_ok=True)

        if self._audit_path.exists():
            self._audit_path.unlink()

        sources = discover_silver_csvs(self._silver_dir)
        if not sources:
            _LOG.warning("No silver CSV files found.")
            pd.DataFrame(
                columns=[
                    "source_file",
                    "rows_processed",
                    "rows_rejected",
                    "rows_warning",
                    "rows_valid",
                    "warning_counts",
                    "severity_distribution",
                    "duplicate_transaction_count",
                    "validation_duration_seconds",
                    "validation_batch_id",
                ]
            ).to_csv(self._summary_path, index=False, encoding="utf-8")
            return []

        summaries: list[ValidationFileSummary] = []
        for path in sources:
            summaries.append(self._validate_file(path))
        pd.DataFrame([vars(s) for s in summaries]).to_csv(
            self._summary_path, index=False, encoding="utf-8"
        )
        _LOG.info("Wrote validation summary to %s", self._summary_path)
        return summaries

    def _validate_file(self, path: Path) -> ValidationFileSummary:
        start = time.perf_counter()
        source_name = path.name
        ts = datetime.now(timezone.utc).isoformat()

        df = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8",
            encoding_errors="replace",
            on_bad_lines="skip",
        )
        df = df.reset_index(drop=True)
        n = len(df)
        row_ids = self._row_identifiers(df)

        reject_floor = str(self._rules.get("reject_from_severity", "S4")).upper()
        reject_rank = _sev_rank(reject_floor)

        rule_masks = self._build_rule_masks(df)
        max_rank, triggered = self._aggregate_issues(df, rule_masks, n)
        status = self._classify_rows(max_rank, reject_rank)

        self._emit_audit_log(source_name, ts, row_ids, rule_masks, status)

        df_out = df.copy()
        df_out["validation_timestamp"] = ts
        df_out["validation_severity"] = [_rank_to_code(int(r)) for r in max_rank]
        df_out["validation_status"] = status
        df_out["validation_rule_triggered"] = triggered

        valid_m = pd.Series(status, index=df.index) == "VALID"
        warn_m = pd.Series(status, index=df.index) == "WARNING"
        rej_m = pd.Series(status, index=df.index) == "REJECTED"

        stem = _normalize_stem(path)
        batch = self.processing_batch_id

        gold_path = self._gold_dir / f"{stem}__{batch}.csv"
        rej_path = self._rejects_dir / f"{stem}_rejected__{batch}.csv"
        warn_path = self._warnings_dir / f"{stem}_warnings__{batch}.csv"

        df_out.loc[valid_m].to_csv(gold_path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL)
        df_out.loc[rej_m].to_csv(rej_path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL)
        df_out.loc[warn_m].to_csv(warn_path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL)

        dup_count = int(rule_masks["duplicate_transaction_id"][0].sum())

        codes = pd.Series([_rank_to_code(int(r)) for r in max_rank])
        sev_dist = {f"S{k}": int((codes == f"S{k}").sum()) for k in range(6)}
        sev_str = ";".join(f"{k}={v}" for k, v in sev_dist.items())

        duration = time.perf_counter() - start
        summary = ValidationFileSummary(
            source_file=source_name,
            rows_processed=n,
            rows_rejected=int(rej_m.sum()),
            rows_warning=int(warn_m.sum()),
            rows_valid=int(valid_m.sum()),
            warning_counts=int(warn_m.sum()),
            severity_distribution=sev_str,
            duplicate_transaction_count=dup_count,
            validation_duration_seconds=round(duration, 4),
            validation_batch_id=self.processing_batch_id,
        )
        _LOG.info(
            "Validated %s: valid=%d warning=%d rejected=%d (%.2fs)",
            source_name,
            summary.rows_valid,
            summary.rows_warning,
            summary.rows_rejected,
            duration,
        )
        return summary

    def _row_identifiers(self, df: pd.DataFrame) -> pd.Series:
        fm = self._rules.get("field_mapping") or {}
        tid_col = fm.get("transaction_id", "txn_id")
        if tid_col in df.columns:
            s = df[tid_col].fillna("").astype(str).str.strip()
            fb = pd.Series([f"__row_{i}" for i in range(len(df))], index=df.index, dtype=object)
            return s.mask(s.eq("") | s.str.lower().isin({"nan", "none"}), fb)
        return pd.Series([f"__row_{i}" for i in range(len(df))], index=df.index, dtype=object)

    def _aggregate_issues(
        self,
        df: pd.DataFrame,
        rule_masks: dict[str, tuple[pd.Series, str]],
        n: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        max_rank = np.zeros(n, dtype=np.int16)
        triggers: list[list[str]] = [[] for _ in range(n)]

        for rule_name, (mask, sev) in rule_masks.items():
            rank = _sev_rank(sev)
            m = mask.fillna(False).to_numpy(dtype=bool)
            max_rank = np.where(m, np.maximum(max_rank, rank), max_rank)
            if m.any():
                pos = np.flatnonzero(m)
                for i in pos:
                    triggers[i].append(rule_name)

        trig_str = np.array(["|".join(sorted(set(t))) if t else "" for t in triggers], dtype=object)
        return max_rank, trig_str

    def _classify_rows(self, max_rank: np.ndarray, reject_rank: int) -> np.ndarray:
        """Classify each row: VALID, WARNING (non-fatal issues), or REJECTED (>= reject floor)."""
        status = np.empty(len(max_rank), dtype=object)
        for i, r in enumerate(max_rank):
            if r == 0:
                status[i] = "VALID"
            elif r >= reject_rank:
                status[i] = "REJECTED"
            else:
                status[i] = "WARNING"
        return status

    def _build_rule_masks(self, df: pd.DataFrame) -> dict[str, tuple[pd.Series, str]]:
        """Return mapping rule_name -> (boolean mask, severity code)."""
        fm = self._rules.get("field_mapping") or {}
        masks: dict[str, tuple[pd.Series, str]] = {}

        def col(name: str, default: Optional[str] = None) -> pd.Series:
            if name in df.columns:
                return df[name]
            return pd.Series(default if default is not None else "", index=df.index, dtype=object)

        # --- Mandatory fields (S5) ---
        tid = col(fm.get("transaction_id", "txn_id")).fillna("").astype(str).str.strip()
        masks["mandatory_transaction_id"] = (tid.eq("") | tid.str.lower().isin({"nan", "none"})), "S5"

        tdate = col(fm.get("transaction_date", "txn_date_parsed")).fillna("").astype(str).str.strip()
        tdate_fb = col(fm.get("transaction_date_raw_fallback", "txn_date")).fillna("").astype(str).str.strip()
        masks["mandatory_transaction_date"] = (
            tdate.eq("") | tdate.str.lower().isin({"nan", "none", "nat"})
        ) & (tdate_fb.eq("") | tdate_fb.str.lower().isin({"nan", "none"})), "S5"

        amt_primary = col(fm.get("transaction_amount", "messy_amount_amount_numeric"))
        amt_fb = col(fm.get("transaction_amount_fallback", "amount_inr_amount_numeric"))
        amt = pd.to_numeric(amt_primary, errors="coerce")
        amt = amt.fillna(pd.to_numeric(amt_fb, errors="coerce"))
        masks["mandatory_transaction_amount"] = amt.isna(), "S5"

        c1 = col(fm.get("currency_code_primary", "messy_amount_currency_code")).fillna("").astype(str).str.strip()
        c2 = col(fm.get("currency_code_secondary", "currency_code")).fillna("").astype(str).str.strip()
        cc = c1.mask(c1.eq("") | c1.str.lower().isin({"nan", "none"}), c2)
        masks["mandatory_currency_code"] = cc.eq("") | cc.str.lower().isin({"nan", "none"}), "S5"

        aid = col(fm.get("account_id", "primary_account_number_standardized")).fillna("").astype(str).str.strip()
        aid_fb = col(fm.get("account_id_fallback", "primary_account_number")).fillna("").astype(str).str.strip()
        aid_f = aid.mask(aid.eq("") | aid.str.lower().isin({"nan", "none"}), aid_fb)
        masks["mandatory_account_id"] = aid_f.eq("") | aid_f.str.lower().isin({"nan", "none"}), "S5"

        # --- Dates ---
        dcfg = self._rules.get("dates") or {}
        dt = pd.to_datetime(tdate, errors="coerce", utc=True)
        now = pd.Timestamp.now(tz=timezone.utc)
        masks["date_future"] = dt.notna() & (dt > now), str(dcfg.get("future_date_severity", "S3"))

        y = dt.dt.year
        low = int(dcfg.get("impossible_year_low", 1900))
        high = int(dcfg.get("impossible_year_high", 2100))
        masks["date_impossible"] = dt.notna() & ((y < low) | (y > high)), str(dcfg.get("impossible_date_severity", "S5"))

        parse_fail_col = "txn_date_parse_failed"
        if parse_fail_col in df.columns:
            pf = col(parse_fail_col).astype(str).str.lower().isin({"true", "1", "yes"})
            masks["date_parse_failed"] = pf & ~tdate_fb.eq(""), str(dcfg.get("parse_failed_with_raw_severity", "S4"))

        # --- Amounts ---
        acfg = self._rules.get("amount") or {}
        max_abs = float(acfg.get("max_abs_value", 1e15))
        masks["amount_non_positive"] = amt.notna() & (amt <= 0), str(acfg.get("non_positive_severity", "S3"))
        masks["amount_overflow"] = amt.notna() & (amt.abs() > max_abs), str(acfg.get("overflow_severity", "S4"))

        pfail = col("messy_amount_amount_parse_failed").astype(str).str.lower().isin({"true", "1", "yes"})
        masks["amount_parse_failed"] = pfail, str(acfg.get("parse_failed_severity", "S4"))

        # --- FX ---
        fcfg = self._rules.get("fx") or {}
        sup = set(str(x).upper() for x in (self._rules.get("supported_currencies") or []))
        fcur = col(fcfg.get("from_currency_column", "from_currency")).fillna("").astype(str).str.upper().str.strip()
        tcur = col(fcfg.get("to_currency_column", "to_currency")).fillna("").astype(str).str.upper().str.strip()
        xr = pd.to_numeric(col(fcfg.get("exchange_rate_column", "exchange_rate")), errors="coerce")
        pair = fcur.ne("") & tcur.ne("") & fcur.ne(tcur)
        masks["fx_missing_rate"] = pair & (xr.isna() | (xr <= 0)), str(fcfg.get("missing_rate_when_pair_severity", "S3"))
        masks["fx_invalid_rate"] = pair & xr.notna() & (xr <= 0), str(fcfg.get("invalid_rate_severity", "S3"))
        bad_f = fcur.ne("") & ~fcur.isin(sup)
        bad_t = tcur.ne("") & ~tcur.isin(sup)
        masks["fx_unsupported_currency"] = bad_f | bad_t, str(fcfg.get("unsupported_currency_severity", "S2"))

        # --- Identifiers ---
        icfg = self._rules.get("identifiers") or {}
        sev_id = str(icfg.get("malformed_identifier_severity", "S3"))
        ifsc_pat = str(icfg.get("ifsc_pattern", "^[A-Z]{4}0[A-Z0-9]{6}$"))
        ifsc = col(icfg.get("ifsc_column", "destination_ifsc_standardized")).fillna("").astype(str).str.upper().str.strip()
        ifsc = ifsc.mask(ifsc.eq(""), col(icfg.get("ifsc_fallback", "destination_ifsc")).fillna("").astype(str).str.upper().str.strip())
        masks["identifier_ifsc_malformed"] = (
            ifsc.ne("") & ~ifsc.str.fullmatch(ifsc_pat, na=False)
        ), sev_id

        bic_pat = str(icfg.get("bic_pattern", "^[A-Z0-9]{8}([A-Z0-9]{3})?$"))
        bic_cols = icfg.get("bic_columns") or ["swift_code_standardized", "counterparty_bic_standardized"]
        bic_fbs = icfg.get("bic_fallbacks") or ["swift_code", "counterparty_bic"]
        bic_bad = pd.Series(False, index=df.index)
        for bc, bf in zip(bic_cols, bic_fbs, strict=False):
            s = col(bc).fillna("").astype(str).str.upper().str.strip()
            s = s.mask(s.eq(""), col(bf).fillna("").astype(str).str.upper().str.strip())
            bic_bad = bic_bad | (s.ne("") & ~s.str.fullmatch(bic_pat, na=False))
        masks["identifier_bic_malformed"] = bic_bad, sev_id

        br = col(icfg.get("branch_column", "branch_code_standardized")).fillna("").astype(str).str.strip()
        br = br.mask(br.eq(""), col(icfg.get("branch_fallback", "branch_code")).fillna("").astype(str).str.strip())
        min_len = int(icfg.get("branch_min_length", 4))
        masks["identifier_branch_short"] = br.ne("") & (br.str.len() < min_len), sev_id

        # --- Enums ---
        enums = self._rules.get("enums") or {}
        for ename, spec in enums.items():
            if not isinstance(spec, Mapping):
                continue
            cname = spec.get("column")
            if not cname or cname not in df.columns:
                continue
            allowed = {
                str(x).upper()
                for x in (spec.get("allowed") or [])
                if str(x).strip() != ""
            }
            allow_blank = bool(spec.get("allow_blank", True))
            sev_e = str(spec.get("severity", "S3"))
            vals = df[cname].fillna("").astype(str).str.strip().str.upper()
            ok = vals.isin(allowed)
            if allow_blank:
                ok = ok | vals.eq("")
            masks[f"enum_invalid_{ename}"] = ~ok, sev_e

        # --- Data quality ---
        dq = self._rules.get("data_quality") or {}
        dup_sev = str(dq.get("duplicate_transaction_severity", "S2"))
        tid_for_dup = tid.mask(tid.eq(""), pd.Series(np.arange(len(df))).astype(str).radd("__row_"))
        vc = tid_for_dup.value_counts()
        dup_mask = tid_for_dup.map(vc) > 1
        masks["duplicate_transaction_id"] = dup_mask, dup_sev

        null_thresh = float(dq.get("null_density_threshold", 0.85))
        null_sev = str(dq.get("null_density_severity", "S1"))
        is_empty = df.eq("") | df.isna() | df.astype(str).apply(lambda s: s.str.lower().isin({"nan", "none", "nat"}))
        null_pct = is_empty.mean(axis=1)
        masks["null_density_high"] = null_pct >= null_thresh, null_sev

        ph_tokens = [str(x).upper() for x in (dq.get("placeholder_tokens") or [])]
        ph_sev = str(dq.get("placeholder_severity", "S2"))
        stacked = df.astype(str).apply(lambda s: s.str.upper().str.strip())
        ph_mask = pd.Series(False, index=df.index)
        for tok in ph_tokens:
            if not tok:
                continue
            ph_mask = ph_mask | stacked.eq(tok).any(axis=1)
        masks["placeholder_value"] = ph_mask, ph_sev

        rep = str(dq.get("unicode_replacement_char", "\ufffd"))
        uni_sev = str(dq.get("unicode_issue_severity", "S2"))
        uni = pd.Series(False, index=df.index)
        for c in df.columns:
            uni = uni | df[c].astype(str).str.contains(rep, regex=False, na=False)
        masks["unicode_replacement"] = uni, uni_sev

        return masks

    def _emit_audit_log(
        self,
        source_name: str,
        ts: str,
        row_ids: pd.Series,
        rule_masks: dict[str, tuple[pd.Series, str]],
        status: np.ndarray,
    ) -> None:
        """Append audit rows for each triggered rule; ``validation_status`` is the row outcome."""
        rows_out: list[dict[str, Any]] = []
        for rule_name, (mask, sev) in rule_masks.items():
            m = mask.fillna(False).to_numpy(dtype=bool)
            if not m.any():
                continue
            for pos in np.flatnonzero(m):
                rows_out.append(
                    {
                        "validation_timestamp": ts,
                        "source_file": source_name,
                        "row_identifier": str(row_ids.iloc[pos]),
                        "rule_name": rule_name,
                        "severity": sev,
                        "validation_status": str(status[pos]),
                        "issue_description": f"Rule {rule_name} triggered ({sev}).",
                    }
                )

        if not rows_out:
            return
        aud = pd.DataFrame(rows_out)
        write_header = not self._audit_path.exists() or self._audit_path.stat().st_size == 0
        aud.to_csv(
            self._audit_path,
            mode="a",
            header=write_header,
            index=False,
            encoding="utf-8",
        )
