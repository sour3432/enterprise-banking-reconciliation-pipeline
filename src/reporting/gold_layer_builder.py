"""
Gold analytical marts — curated business-ready datasets after reconciliation.

Writes mart CSVs under ``data/gold/`` (prefixed ``gold_``) without mutating
per-feed validated extracts produced by the validation engine.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from reconciliation.duplicate_engine import _first_col, discover_gold_csvs
from utils.logger import get_logger

_LOG = get_logger("gold_layer")


def _resolve_path(project_root: Path, configured: str | Path) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (project_root / path)


def _series_txn_id(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("txn_id_standardized", "transaction_id", "txn_id"))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    out = s.fillna("").astype(str).str.strip()
    return out.mask(out.str.lower().isin({"nan", "none", ""}), "")


def _series_amount(df: pd.DataFrame) -> pd.Series:
    s = _first_col(
        df,
        (
            "amount_inr_amount_numeric",
            "messy_amount_amount_numeric",
            "amount",
            "converted_amount_amount_numeric",
        ),
    )
    if s is None:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(s, errors="coerce")


def _series_currency(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("currency_code", "messy_amount_currency_code", "currency"))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.strip().str.upper()


def _parse_kv_semicolon(s: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not s or not str(s).strip():
        return out
    for part in str(s).split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[k.strip()] = int(float(v.strip()))
        except ValueError:
            continue
    return out


_DUP_RANK = {
    "EXACT_DUPLICATE": 5,
    "RETRY_PATTERN": 4,
    "FUZZY_DUPLICATE": 3,
    "POSSIBLE_DUPLICATE": 2,
    "NOT_DUPLICATE": 0,
}


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))
        self.sz = [1] * n

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.sz[ra] < self.sz[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        self.sz[ra] += self.sz[rb]


def _duplicate_row_status(dup_detail: pd.DataFrame, n_rows: int) -> tuple[pd.Series, int]:
    """Per pipeline row: strongest duplicate classification touching that row; duplicate cluster count (paired components)."""
    status = np.array(["NOT_DUPLICATE"] * n_rows, dtype=object)
    if dup_detail is None or dup_detail.empty or n_rows == 0:
        return pd.Series(status), 0
    uf = _UnionFind(n_rows)
    touched: set[int] = set()
    for _, r in dup_detail.iterrows():
        try:
            a = int(float(r.get("row_index_a", -1)))
            b = int(float(r.get("row_index_b", -1)))
        except (TypeError, ValueError):
            continue
        cls = str(r.get("duplicate_classification", "") or "").strip()
        if cls not in _DUP_RANK:
            continue
        for ix in (a, b):
            if 0 <= ix < n_rows:
                if _DUP_RANK.get(str(status[ix]), 0) < _DUP_RANK[cls]:
                    status[ix] = cls
                touched.add(ix)
        if 0 <= a < n_rows and 0 <= b < n_rows:
            uf.union(a, b)
    if not touched:
        return pd.Series(status), 0
    roots = {uf.find(i) for i in touched}
    return pd.Series(status), len(roots)


@dataclass
class GoldLayerBuilder:
    """
    Build analytical gold marts for the current processing batch.

    Args:
        project_root: Repository root.
        config: Parsed ``config.yaml``.
        processing_batch_id: Validation / gold feed batch id (``*__{id}.csv``).
        pipeline_duration_seconds: End-to-end pipeline wall time for reporting.
    """

    project_root: Path
    config: Mapping[str, Any]
    processing_batch_id: str | None = None
    pipeline_duration_seconds: float = 0.0
    mart_paths: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        gl = self.config.get("gold_layer") or {}
        self._gold_dir = _resolve_path(self.project_root, self.config.get("gold_path", "data/gold"))
        self._txn_master = self._gold_dir / gl.get("transaction_master_filename", "gold_transaction_master.csv")
        self._dup_sum = self._gold_dir / gl.get("duplicate_summary_filename", "gold_duplicate_summary.csv")
        self._val_sum = self._gold_dir / gl.get("validation_summary_filename", "gold_validation_summary.csv")
        self._rec_sum = self._gold_dir / gl.get("reconciliation_summary_filename", "gold_reconciliation_summary.csv")
        self._fx_sum = self._gold_dir / gl.get("fx_variance_summary_filename", "gold_fx_variance_summary.csv")

        out_base = _resolve_path(self.project_root, self.config.get("output_path", "outputs"))
        val_cfg = self.config.get("validation") or {}
        self._validation_summary_path = out_base / val_cfg.get(
            "validation_summary_path", "profiling_reports/validation_summary.csv"
        )
        self._validation_audit_path = _resolve_path(
            self.project_root, self.config.get("audit_path", "data/audit")
        ) / val_cfg.get("validation_audit_log_filename", "validation_audit_log.csv")

        dup_cfg = self.config.get("duplicate_intelligence") or {}
        self._dup_detail_path = out_base / dup_cfg.get(
            "duplicate_detail_path", "profiling_reports/duplicate_intelligence_detailed.csv"
        )
        self._dup_prof_summary_path = out_base / dup_cfg.get(
            "duplicate_summary_path", "profiling_reports/duplicate_intelligence_summary.csv"
        )

        rec_cfg = self.config.get("reconciliation") or {}
        self._rec_audit_path = _resolve_path(
            self.project_root, self.config.get("audit_path", "data/audit")
        ) / rec_cfg.get("reconciliation_audit_log_filename", "reconciliation_audit_log.csv")
        self._rec_summary_path = out_base / rec_cfg.get(
            "reconciliation_summary_path", "reconciliation_reports/reconciliation_summary.csv"
        )

    def run(self) -> dict[str, Path]:
        self._gold_dir.mkdir(parents=True, exist_ok=True)
        batch = self.processing_batch_id or ""
        paths = discover_gold_csvs(self._gold_dir, self.processing_batch_id)
        frames: list[pd.DataFrame] = []
        for p in paths:
            df = pd.read_csv(
                p,
                dtype=str,
                keep_default_na=False,
                encoding="utf-8",
                encoding_errors="replace",
                on_bad_lines="skip",
            )
            df["_source_file"] = p.name
            frames.append(df)

        if not frames:
            master = pd.DataFrame(
                columns=[
                    "pipeline_row_id",
                    "transaction_id",
                    "standardized_amount",
                    "currency_code",
                    "reconciliation_status",
                    "validation_status",
                    "duplicate_status",
                    "reconciliation_confidence",
                    "matched_transaction_id",
                    "reconciliation_category",
                    "matching_logic_used",
                    "ingestion_timestamp",
                    "standardization_timestamp",
                    "validation_timestamp",
                    "reconciliation_timestamp",
                    "processing_batch_id",
                    "source_gold_file",
                ]
            )
            master.to_csv(self._txn_master, index=False, encoding="utf-8")
            self._write_empty_marts(batch)
            self.mart_paths = {
                "transaction_master": self._txn_master,
                "duplicate_summary": self._dup_sum,
                "validation_summary": self._val_sum,
                "reconciliation_summary": self._rec_sum,
                "fx_variance_summary": self._fx_sum,
            }
            _LOG.warning("Gold layer: no feed gold files for batch %s; wrote empty marts.", batch)
            return self.mart_paths

        master_raw = pd.concat(frames, ignore_index=True)
        n = len(master_raw)
        master_raw["pipeline_row_id"] = np.arange(n, dtype=np.int64)

        rec_aud = self._safe_read_csv(self._rec_audit_path)
        if not rec_aud.empty and "pipeline_row_id" in rec_aud.columns:
            rec_aud["pipeline_row_id"] = pd.to_numeric(rec_aud["pipeline_row_id"], errors="coerce").astype("Int64")
            m = master_raw.merge(
                rec_aud,
                on="pipeline_row_id",
                how="left",
                suffixes=("", "_rec"),
            )
        elif not rec_aud.empty and len(rec_aud) == n:
            m = master_raw.reset_index(drop=True)
            rec_aud = rec_aud.reset_index(drop=True)
            for c in rec_aud.columns:
                m[c] = rec_aud[c]
        else:
            m = master_raw.copy()
            m["reconciliation_type"] = "MISSING_SETTLEMENT"
            m["confidence_score"] = 0
            m["matched_transaction_id"] = ""
            m["reconciliation_category"] = ""
            m["matching_logic_used"] = "no_reconciliation_audit_or_length_mismatch"
            m["reconciliation_timestamp"] = ""

        dup_detail = self._safe_read_csv(self._dup_detail_path)
        dup_status, dup_cluster_count = _duplicate_row_status(dup_detail, n)

        tid = _series_txn_id(m)
        amt = _series_amount(m)
        ccy = _series_currency(m)
        val_st = (
            m["validation_status"]
            if "validation_status" in m.columns
            else pd.Series(["UNKNOWN"] * n, index=m.index)
        )
        rec_st = m["reconciliation_type"] if "reconciliation_type" in m.columns else pd.Series(["UNKNOWN"] * n)
        rec_conf = pd.to_numeric(m.get("confidence_score", 0), errors="coerce").fillna(0).astype(int)
        rec_line = m.get("matching_logic_used", pd.Series([""] * n)).fillna("").astype(str)
        rec_cat = m.get("reconciliation_category", pd.Series([""] * n)).fillna("").astype(str)
        matched = m.get("matched_transaction_id", pd.Series([""] * n)).fillna("").astype(str)
        rec_ts = m.get("reconciliation_timestamp", pd.Series([""] * n)).fillna("").astype(str)

        ingest_ts = m.get("ingestion_timestamp", pd.Series([""] * n)).fillna("").astype(str)
        std_ts = m.get("standardization_timestamp", pd.Series([""] * n)).fillna("").astype(str)
        val_ts = m.get("validation_timestamp", pd.Series([""] * n)).fillna("").astype(str)

        txn_master = pd.DataFrame(
            {
                "pipeline_row_id": m["pipeline_row_id"],
                "transaction_id": tid,
                "standardized_amount": amt,
                "currency_code": ccy,
                "reconciliation_status": rec_st,
                "validation_status": val_st,
                "duplicate_status": dup_status.values,
                "reconciliation_confidence": rec_conf,
                "matched_transaction_id": matched,
                "reconciliation_category": rec_cat,
                "matching_logic_used": rec_line,
                "ingestion_timestamp": ingest_ts,
                "standardization_timestamp": std_ts,
                "validation_timestamp": val_ts,
                "reconciliation_timestamp": rec_ts,
                "processing_batch_id": batch,
                "source_gold_file": m["_source_file"],
            }
        )
        txn_master.to_csv(self._txn_master, index=False, encoding="utf-8")

        self._write_duplicate_summary(batch, dup_detail, dup_cluster_count)
        self._write_validation_summary(batch)
        self._write_reconciliation_summary(batch)
        self._write_fx_variance(m)

        self.mart_paths = {
            "transaction_master": self._txn_master,
            "duplicate_summary": self._dup_sum,
            "validation_summary": self._val_sum,
            "reconciliation_summary": self._rec_sum,
            "fx_variance_summary": self._fx_sum,
        }
        _LOG.info(
            "Gold layer marts written (%d rows) for batch %s.",
            n,
            batch,
        )
        return self.mart_paths

    def _write_empty_marts(self, batch: str) -> None:
        pd.DataFrame(
            columns=[
                "processing_batch_id",
                "duplicate_type",
                "pair_count",
                "avg_confidence",
                "duplicate_cluster_count",
                "retry_pattern_counts",
            ]
        ).to_csv(self._dup_sum, index=False, encoding="utf-8")
        pd.DataFrame(
            columns=[
                "processing_batch_id",
                "total_rows_processed",
                "total_valid",
                "total_warning",
                "total_rejected",
                "severity_S0",
                "severity_S1",
                "severity_S2",
                "severity_S3",
                "severity_S4",
                "severity_S5",
                "validation_rule_trigger_count",
                "validation_unique_rules_triggered",
                "pipeline_duration_seconds",
            ]
        ).to_csv(self._val_sum, index=False, encoding="utf-8")
        cols = [
            "processing_batch_id",
            "FULL_MATCH",
            "PARTIAL_MATCH",
            "DATE_MISMATCH",
            "AMOUNT_MISMATCH",
            "MISSING_SETTLEMENT",
            "MULTIPLE_MATCHES",
            "total_rows",
            "total_matched",
            "unmatched",
            "amount_variance_total",
            "pipeline_duration_seconds",
        ]
        row = {c: 0 for c in cols}
        row["processing_batch_id"] = batch
        pd.DataFrame([row]).to_csv(self._rec_sum, index=False, encoding="utf-8")
        pd.DataFrame(
            columns=[
                "pipeline_row_id",
                "transaction_id",
                "currency_pair",
                "treasury_rate",
                "transaction_rate",
                "variance_pct",
                "stale_fx_indicator",
                "trade_timestamp",
                "processing_batch_id",
            ]
        ).to_csv(self._fx_sum, index=False, encoding="utf-8")

    def _safe_read_csv(self, path: Path) -> pd.DataFrame:
        if not path.is_file():
            return pd.DataFrame()
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8", on_bad_lines="skip")
        except Exception:
            _LOG.warning("Could not read %s", path)
            return pd.DataFrame()

    def _write_duplicate_summary(self, batch: str, dup_detail: pd.DataFrame, cluster_count: int) -> None:
        prof = self._safe_read_csv(self._dup_prof_summary_path)
        retry_total = 0
        if not prof.empty and "retry_pattern_pairs" in prof.columns:
            try:
                retry_total = int(float(prof.iloc[0]["retry_pattern_pairs"]))
            except (ValueError, TypeError):
                retry_total = 0
        rows: list[dict[str, Any]] = []
        if dup_detail is not None and not dup_detail.empty and "duplicate_classification" in dup_detail.columns:
            g = dup_detail.groupby("duplicate_classification", dropna=False)
            for cls, grp in g:
                conf = pd.to_numeric(grp.get("duplicate_confidence", 0), errors="coerce")
                rows.append(
                    {
                        "processing_batch_id": batch,
                        "duplicate_type": str(cls),
                        "pair_count": int(len(grp)),
                        "avg_confidence": round(float(conf.mean()), 2) if len(conf) else 0.0,
                        "duplicate_cluster_count": int(cluster_count),
                        "retry_pattern_counts": int(retry_total),
                    }
                )
        if not rows:
            rows.append(
                {
                    "processing_batch_id": batch,
                    "duplicate_type": "NONE",
                    "pair_count": 0,
                    "avg_confidence": 0.0,
                    "duplicate_cluster_count": 0,
                    "retry_pattern_counts": retry_total,
                }
            )
        pd.DataFrame(rows).to_csv(self._dup_sum, index=False, encoding="utf-8")

    def _write_validation_summary(self, batch: str) -> None:
        vs = self._safe_read_csv(self._validation_summary_path)
        sev_tot = {f"severity_S{k}": 0 for k in range(6)}
        rule_triggers = 0
        if not vs.empty and "validation_batch_id" in vs.columns:
            vsb = vs[vs["validation_batch_id"].astype(str) == str(batch)]
            if vsb.empty:
                vsb = vs
        else:
            vsb = vs
        tot_proc = int(pd.to_numeric(vsb.get("rows_processed", 0), errors="coerce").fillna(0).sum())
        tot_val = int(pd.to_numeric(vsb.get("rows_valid", 0), errors="coerce").fillna(0).sum())
        tot_warn = int(pd.to_numeric(vsb.get("rows_warning", 0), errors="coerce").fillna(0).sum())
        tot_rej = int(pd.to_numeric(vsb.get("rows_rejected", 0), errors="coerce").fillna(0).sum())
        for _, r in vsb.iterrows():
            dist = _parse_kv_semicolon(str(r.get("severity_distribution", "")))
            for k, v in dist.items():
                key = f"severity_{k.strip()}"
                if key in sev_tot:
                    sev_tot[key] += int(v)
        aud = self._safe_read_csv(self._validation_audit_path)
        if not aud.empty and "rule_name" in aud.columns:
            rule_triggers = int(len(aud))
            unique_rules = int(aud["rule_name"].nunique())
        else:
            unique_rules = 0

        out = {
            "processing_batch_id": batch,
            "total_rows_processed": tot_proc,
            "total_valid": tot_val,
            "total_warning": tot_warn,
            "total_rejected": tot_rej,
            "validation_rule_trigger_count": rule_triggers,
            "validation_unique_rules_triggered": unique_rules,
            "pipeline_duration_seconds": round(float(self.pipeline_duration_seconds), 4),
            **sev_tot,
        }
        pd.DataFrame([out]).to_csv(self._val_sum, index=False, encoding="utf-8")

    def _write_reconciliation_summary(self, batch: str) -> None:
        rs = self._safe_read_csv(self._rec_summary_path)
        keys = ["FULL_MATCH", "PARTIAL_MATCH", "DATE_MISMATCH", "AMOUNT_MISMATCH", "MISSING_SETTLEMENT", "MULTIPLE_MATCHES"]
        base = {k: 0 for k in keys}
        if not rs.empty:
            if "processing_batch_id" in rs.columns and batch:
                rsf = rs[rs["processing_batch_id"].astype(str) == str(batch)]
                if rsf.empty:
                    rsf = rs.tail(1)
            else:
                rsf = rs.tail(1)
            row = rsf.iloc[0].to_dict()
            dist = _parse_kv_semicolon(str(row.get("reconciliation_distribution", "")))
            for k in keys:
                base[k] = int(dist.get(k, 0))
            out = {
                "processing_batch_id": batch,
                **base,
                "total_rows": int(float(row.get("total_rows", 0) or 0)),
                "total_matched": int(float(row.get("total_matched", 0) or 0)),
                "unmatched": int(float(row.get("unmatched", 0) or 0)),
                "amount_variance_total": float(row.get("amount_variance_total", 0) or 0),
                "pipeline_duration_seconds": round(float(self.pipeline_duration_seconds), 4),
            }
        else:
            out = {
                "processing_batch_id": batch,
                **base,
                "total_rows": 0,
                "total_matched": 0,
                "unmatched": 0,
                "amount_variance_total": 0.0,
                "pipeline_duration_seconds": round(float(self.pipeline_duration_seconds), 4),
            }
        pd.DataFrame([out]).to_csv(self._rec_sum, index=False, encoding="utf-8")

    def _write_fx_variance(self, m: pd.DataFrame) -> None:
        batch = self.processing_batch_id or ""
        rows: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        if "from_currency" in m.columns and "to_currency" in m.columns:
            fc = m["from_currency"].fillna("").astype(str).str.upper().str.strip()
            tc = m["to_currency"].fillna("").astype(str).str.upper().str.strip()
            xr = pd.to_numeric(m.get("exchange_rate"), errors="coerce")
            rr = pd.to_numeric(m.get("reference_rate"), errors="coerce")
            ttp = pd.to_datetime(m.get("trade_timestamp_parsed"), errors="coerce", utc=True)
            ttf = m.get("trade_timestamp_parse_failed", pd.Series(["false"] * len(m)))
            stale_days = 90.0
            tid_all = _series_txn_id(m)
            prid = m["pipeline_row_id"] if "pipeline_row_id" in m.columns else pd.Series(np.arange(len(m)))
            for pos in range(len(m)):
                if not fc.iloc[pos] or not tc.iloc[pos] or fc.iloc[pos] == tc.iloc[pos]:
                    continue
                if pd.isna(xr.iloc[pos]) or pd.isna(rr.iloc[pos]) or float(rr.iloc[pos]) == 0.0:
                    continue
                var_pct = (float(xr.iloc[pos]) - float(rr.iloc[pos])) / float(rr.iloc[pos]) * 100.0
                stale = False
                if str(ttf.iloc[pos]).lower() in {"true", "1", "yes"}:
                    stale = True
                elif pd.notna(ttp.iloc[pos]):
                    age = (now - ttp.iloc[pos]).days
                    if age > stale_days:
                        stale = True
                rows.append(
                    {
                        "pipeline_row_id": int(prid.iloc[pos]),
                        "transaction_id": str(tid_all.iloc[pos]),
                        "currency_pair": f"{fc.iloc[pos]}/{tc.iloc[pos]}",
                        "treasury_rate": float(rr.iloc[pos]),
                        "transaction_rate": float(xr.iloc[pos]),
                        "variance_pct": round(var_pct, 6),
                        "stale_fx_indicator": bool(stale),
                        "trade_timestamp": str(ttp.iloc[pos]) if pd.notna(ttp.iloc[pos]) else "",
                        "processing_batch_id": batch,
                    }
                )
        fx_cols = [
            "pipeline_row_id",
            "transaction_id",
            "currency_pair",
            "treasury_rate",
            "transaction_rate",
            "variance_pct",
            "stale_fx_indicator",
            "trade_timestamp",
            "processing_batch_id",
        ]
        pd.DataFrame(rows, columns=fx_cols).to_csv(self._fx_sum, index=False, encoding="utf-8")
