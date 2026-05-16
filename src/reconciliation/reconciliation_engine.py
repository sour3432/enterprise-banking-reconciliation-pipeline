"""
Enterprise operational reconciliation across validated gold datasets.

Supports transaction–settlement, refund–original, and FX–treasury rate checks
with deterministic tolerances, confidence scoring, and audit trails.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from utils.logger import get_logger

from .duplicate_engine import (
    _first_col,
    _series_amount,
    _series_customer_id,
    _series_reference,
    _series_timestamp,
    _series_txn_id,
    discover_gold_csvs,
)

_LOG = get_logger("reconciliation")


def _resolve_path(project_root: Path, configured: str | Path) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (project_root / path)


def _normalize_id(raw: str) -> str:
    t = str(raw or "").strip()
    if not t or t.lower() in {"nan", "none"}:
        return ""
    t = re.sub(r"[\s\-_]+", "", t, flags=re.UNICODE)
    return t.upper()


def _series_settlement_ref(df: pd.DataFrame) -> pd.Series:
    s = _first_col(
        df,
        (
            "settlement_reference_standardized",
            "settlement_reference",
        ),
    )
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.strip()


def _series_txn_type(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("txn_type",))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.upper()


def _series_debit_credit(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("debit_credit",))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.upper().str.strip()


def _series_currency(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("currency_code", "messy_amount_currency_code", "currency"))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.strip().str.upper()


def _series_settlement_date(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("settlement_date_parsed", "settlement_date"))
    if s is None:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    return pd.to_datetime(s, errors="coerce", utc=True)


def _series_link_ref(df: pd.DataFrame) -> pd.Series:
    """Reference on refund row pointing to original (reference_number, rrn, bank_ref)."""
    s = _first_col(
        df,
        (
            "reference_number_standardized",
            "reference_number",
            "rrn_standardized",
            "rrn",
            "bank_ref_no_standardized",
            "bank_ref_no",
        ),
    )
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.strip()


def _is_refund_row(txn_type: str, debit_credit: str, amount: float) -> bool:
    tt = (txn_type or "").upper()
    if "REFUND" in tt or "REVERSAL" in tt:
        return True
    if debit_credit == "CREDIT" and not pd.isna(amount) and amount < 0:
        return True
    return False


def _malformed_identifier(s: str, min_len: int = 4) -> bool:
    t = _normalize_id(s)
    if not t:
        return True
    if len(t) < min_len:
        return True
    if not re.fullmatch(r"[A-Z0-9./]+", t):
        return True
    return False


@dataclass
class ReconciliationEngine:
    """
    Deterministic multi-scenario reconciliation over gold extracts.

    Args:
        project_root: Repository root.
        config: Parsed ``config.yaml``.
        processing_batch_id: Optional filter ``*__{id}.csv`` under gold.
    """

    project_root: Path
    config: Mapping[str, Any]
    processing_batch_id: str | None = None

    def __post_init__(self) -> None:
        rec_cfg = self.config.get("reconciliation") or {}
        self._gold_dir = _resolve_path(self.project_root, self.config.get("gold_path", "data/gold"))
        audit_dir = _resolve_path(self.project_root, self.config.get("audit_path", "data/audit"))
        self._audit_path = audit_dir / rec_cfg.get(
            "reconciliation_audit_log_filename", "reconciliation_audit_log.csv"
        )
        out_base = _resolve_path(self.project_root, self.config.get("output_path", "outputs"))
        self._summary_path = out_base / rec_cfg.get(
            "reconciliation_summary_path", "reconciliation_reports/reconciliation_summary.csv"
        )
        self._amount_abs_tol = float(rec_cfg.get("amount_tolerance_abs", 0.01))
        self._amount_pct_tol = float(rec_cfg.get("amount_tolerance_pct", 0.001))
        self._ts_window_hours = float(rec_cfg.get("timestamp_window_hours", 72))
        self._fx_rate_pct_tol = float(rec_cfg.get("fx_rate_tolerance_pct", 0.02))

    def run(self, duplicate_summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._summary_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        if self._audit_path.exists():
            self._audit_path.unlink()

        batch = self.processing_batch_id
        paths = discover_gold_csvs(self._gold_dir, batch)
        if not paths:
            _LOG.warning("No gold files for reconciliation (batch=%s).", batch)
            out = self._empty_summary(batch or "", duplicate_summary)
            pd.DataFrame([out]).to_csv(self._summary_path, index=False, encoding="utf-8")
            return out

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

        df = pd.concat(frames, ignore_index=True)
        n = len(df)
        idx = df.index

        tid = _series_txn_id(df)
        tid_n = tid.map(_normalize_id)
        amt = _series_amount(df)
        ts = _series_timestamp(df)
        cust = _series_customer_id(df)
        ccy = _series_currency(df)
        sref = _series_settlement_ref(df)
        sref_n = sref.map(_normalize_id)
        ssettle_dt = _series_settlement_date(df)
        txn_type = _series_txn_type(df)
        dc = _series_debit_credit(df)
        link_ref = _series_link_ref(df)
        link_n = link_ref.map(_normalize_id)

        fx_from = _first_col(df, ("from_currency",))
        fx_to = _first_col(df, ("to_currency",))
        if fx_from is not None:
            fx_from = fx_from.fillna("").astype(str).str.upper().str.strip()
        else:
            fx_from = pd.Series("", index=idx, dtype=object)
        if fx_to is not None:
            fx_to = fx_to.fillna("").astype(str).str.upper().str.strip()
        else:
            fx_to = pd.Series("", index=idx, dtype=object)
        xr = (
            pd.to_numeric(df["exchange_rate"], errors="coerce")
            if "exchange_rate" in df.columns
            else pd.Series(np.nan, index=idx, dtype=float)
        )
        rr = (
            pd.to_numeric(df["reference_rate"], errors="coerce")
            if "reference_rate" in df.columns
            else pd.Series(np.nan, index=idx, dtype=float)
        )
        conv_id = (
            df["conversion_id"].fillna("").astype(str).str.strip()
            if "conversion_id" in df.columns
            else pd.Series("", index=idx, dtype=object)
        )
        treas_ref = (
            df["treasury_rate_reference"].fillna("").astype(str).str.strip()
            if "treasury_rate_reference" in df.columns
            else pd.Series("", index=idx, dtype=object)
        )

        by_txn: dict[str, list[int]] = {}
        by_sref: dict[str, list[int]] = {}
        for pos, (t, sr) in enumerate(zip(tid_n.tolist(), sref_n.tolist(), strict=False)):
            if t:
                by_txn.setdefault(t, []).append(pos)
            if sr:
                by_sref.setdefault(sr, []).append(pos)

        audit_rows: list[dict[str, Any]] = []
        rec_types: list[str] = []
        confidences: list[int] = []
        variances: list[float] = []

        def within_amount(a: float, b: float) -> bool:
            if pd.isna(a) or pd.isna(b):
                return False
            diff = abs(a - b)
            if diff <= self._amount_abs_tol:
                return True
            base = max(abs(a), abs(b), 1.0)
            return diff / base <= self._amount_pct_tol

        def within_ts(a: pd.Timestamp, b: pd.Timestamp) -> bool:
            if pd.isna(a) or pd.isna(b):
                return False
            h = abs((a - b).total_seconds()) / 3600.0
            return h <= self._ts_window_hours

        for i in idx:
            ts_i = ts.iloc[i] if i < len(ts) else pd.NaT
            cat = ""
            rtype = "MISSING_SETTLEMENT"
            conf = 0
            logic_parts: list[str] = []
            matched_id = ""
            partner: Optional[int] = None

            # --- Refund vs original ---
            if _is_refund_row(txn_type.iloc[i], dc.iloc[i], amt.iloc[i]):
                cat = "REFUND_ORIGINAL"
                lk = link_n.iloc[i]
                if not lk or _malformed_identifier(lk, min_len=3):
                    rtype = "MISSING_SETTLEMENT"
                    conf = 20
                    logic_parts.append("missing_or_malformed_refund_link_reference")
                else:
                    cand = list(
                        dict.fromkeys(
                            [p for p in by_txn.get(lk, []) if p != i]
                            + [p for p in by_sref.get(lk, []) if p != i]
                        )
                    )
                    cand = [p for p in cand if within_amount(amt.iloc[i], amt.iloc[p]) or pd.isna(amt.iloc[i]) or pd.isna(amt.iloc[p])]
                    pref = [p for p in cand if tid_n.iloc[p] == lk]
                    if len(pref) == 1:
                        cand = pref
                    elif len(pref) > 1:
                        cand = pref
                    if not cand:
                        rtype = "MISSING_SETTLEMENT"
                        conf = 25
                        logic_parts.append("refund_link_not_found_on_ledger")
                    elif len(cand) > 1:
                        rtype = "MULTIPLE_MATCHES"
                        conf = 40
                        logic_parts.append("multiple_original_candidates")
                        partner = cand[0]
                        matched_id = tid.iloc[partner]
                    else:
                        partner = cand[0]
                        matched_id = tid.iloc[partner]
                        logic_parts.append("reference_matches_original_txn_or_settlement")
                        a_ok = within_amount(amt.iloc[i], amt.iloc[partner])
                        t_ok = within_ts(ts_i, ts.iloc[partner])
                        if a_ok and t_ok:
                            rtype = "FULL_MATCH"
                            conf = 100
                        elif a_ok and not t_ok:
                            rtype = "DATE_MISMATCH"
                            conf = 70
                        elif not a_ok and t_ok:
                            rtype = "AMOUNT_MISMATCH"
                            conf = 55
                        else:
                            rtype = "PARTIAL_MATCH"
                            conf = 45
                        if partner is not None and not pd.isna(amt.iloc[i]) and not pd.isna(amt.iloc[partner]):
                            variances.append(float(abs(amt.iloc[i] - amt.iloc[partner])))

            # --- FX vs treasury (conversion / treasury-tagged rows only) ---
            elif (
                str(fx_from.iloc[i]).strip()
                and str(fx_to.iloc[i]).strip()
                and str(fx_from.iloc[i]) != str(fx_to.iloc[i])
                and not pd.isna(xr.iloc[i])
                and not pd.isna(rr.iloc[i])
                and float(rr.iloc[i]) != 0.0
                and (
                    str(conv_id.iloc[i]).strip() != ""
                    or str(treas_ref.iloc[i]).strip() != ""
                    or any(
                        k in txn_type.iloc[i]
                        for k in ("FX", "CONVERSION", "SPOT", "FORWARD", "SWAP")
                    )
                )
            ):
                cat = "FX_TREASURY"
                logic_parts.append("fx_pair_with_exchange_and_reference_rate")
                rel = abs(xr.iloc[i] - rr.iloc[i]) / abs(rr.iloc[i])
                if rel <= self._fx_rate_pct_tol:
                    rtype = "FULL_MATCH"
                    conf = 100
                    matched_id = f"INTERNAL_RATE:{xr.iloc[i]:.8f}"
                elif rel <= self._fx_rate_pct_tol * 5:
                    rtype = "PARTIAL_MATCH"
                    conf = 80
                    matched_id = f"INTERNAL_RATE:{xr.iloc[i]:.8f}"
                else:
                    rtype = "AMOUNT_MISMATCH"
                    conf = 50
                    matched_id = f"INTERNAL_RATE:{xr.iloc[i]:.8f}"
                variances.append(float(abs(xr.iloc[i] - rr.iloc[i])))

            # --- Transaction vs settlement ---
            else:
                cat = "TXN_SETTLEMENT"
                sr = sref_n.iloc[i]
                if not sr:
                    rtype = "MISSING_SETTLEMENT"
                    conf = 15
                    logic_parts.append("empty_settlement_reference")
                elif _malformed_identifier(sr):
                    rtype = "PARTIAL_MATCH"
                    conf = 35
                    logic_parts.append("malformed_settlement_reference")
                else:
                    cand = list(
                        dict.fromkeys(
                            [p for p in by_txn.get(sr, []) if p != i]
                            + [p for p in by_sref.get(sr, []) if p != i]
                        )
                    )
                    preferred = [p for p in cand if tid_n.iloc[p] == sr]
                    if len(preferred) == 1:
                        cand = preferred
                    elif len(preferred) > 1:
                        cand = preferred
                    ccy_i = str(ccy.iloc[i]).strip()
                    if ccy_i:
                        cand = [
                            p
                            for p in cand
                            if not str(ccy.iloc[p]).strip()
                            or str(ccy.iloc[p]).strip() == ccy_i
                            or str(ccy.iloc[p]).strip() == ""
                        ]
                    cust_i = str(cust.iloc[i]).strip()
                    if cust_i:
                        boosted = [p for p in cand if str(cust.iloc[p]).strip() == cust_i]
                        if boosted:
                            cand = boosted
                    if not cand:
                        rtype = "MISSING_SETTLEMENT"
                        conf = 30
                        logic_parts.append("settlement_reference_not_found")
                    elif len(cand) > 1:
                        rtype = "MULTIPLE_MATCHES"
                        conf = 45
                        logic_parts.append("duplicate_collision_multiple_settlement_rows")
                        partner = cand[0]
                        matched_id = tid.iloc[partner]
                    else:
                        partner = cand[0]
                        matched_id = tid.iloc[partner]
                        logic_parts.append("settlement_reference_resolved_to_counterparty_txn_id")
                        a_ok = within_amount(amt.iloc[i], amt.iloc[partner])
                        st_dt = ssettle_dt.iloc[i]
                        t_ok = within_ts(ts_i, ts.iloc[partner]) or (
                            not pd.isna(st_dt) and within_ts(st_dt, ts.iloc[partner])
                        )
                        if pd.isna(ts_i) and pd.isna(st_dt):
                            t_ok = True
                            logic_parts.append("null_transaction_timestamps_used_settlement_date_or_lenient")
                        if a_ok and t_ok:
                            rtype = "FULL_MATCH"
                            conf = 100
                        elif a_ok and not t_ok:
                            rtype = "DATE_MISMATCH"
                            conf = 72
                        elif not a_ok and t_ok:
                            rtype = "AMOUNT_MISMATCH"
                            conf = 58
                        else:
                            rtype = "PARTIAL_MATCH"
                            conf = 48
                        if not pd.isna(amt.iloc[i]) and not pd.isna(amt.iloc[partner]):
                            variances.append(float(abs(amt.iloc[i] - amt.iloc[partner])))

            rec_types.append(rtype)
            confidences.append(conf)
            audit_rows.append(
                {
                    "pipeline_row_id": len(audit_rows),
                    "reconciliation_timestamp": datetime.now(timezone.utc).isoformat(),
                    "source_file": df.at[i, "_source_file"],
                    "source_transaction_id": tid.iloc[i],
                    "matched_transaction_id": matched_id,
                    "reconciliation_category": cat,
                    "reconciliation_type": rtype,
                    "confidence_score": conf,
                    "matching_logic_used": ";".join(logic_parts) if logic_parts else "none",
                    "matched_row_index": "" if partner is None else str(int(partner)),
                }
            )

        aud_cols = [
            "pipeline_row_id",
            "reconciliation_timestamp",
            "source_file",
            "source_transaction_id",
            "matched_transaction_id",
            "reconciliation_category",
            "reconciliation_type",
            "confidence_score",
            "matching_logic_used",
            "matched_row_index",
        ]
        aud = pd.DataFrame(audit_rows, columns=aud_cols)
        aud.to_csv(self._audit_path, index=False, encoding="utf-8")

        matched = sum(1 for r in rec_types if r in {"FULL_MATCH", "PARTIAL_MATCH", "DATE_MISMATCH", "AMOUNT_MISMATCH"})
        unmatched = int(n - matched)
        rt_dist: dict[str, int] = {}
        for r in rec_types:
            rt_dist[r] = rt_dist.get(r, 0) + 1
        conf_dist = self._bucket_confidence(confidences)
        dup_exact = int(duplicate_summary.get("exact_duplicate_pairs", 0)) if duplicate_summary else 0
        dup_fuzzy = int(duplicate_summary.get("fuzzy_duplicate_pairs", 0)) if duplicate_summary else 0
        dup_retry = int(duplicate_summary.get("retry_pattern_pairs", 0)) if duplicate_summary else 0
        dup_poss = int(duplicate_summary.get("possible_duplicate_pairs", 0)) if duplicate_summary else 0

        summary = {
            "processing_batch_id": batch or "",
            "total_rows": n,
            "total_matched": matched,
            "unmatched": unmatched,
            "duplicate_exact_pairs": dup_exact,
            "duplicate_fuzzy_pairs": dup_fuzzy,
            "duplicate_retry_pairs": dup_retry,
            "duplicate_possible_pairs": dup_poss,
            "reconciliation_distribution": ";".join(f"{k}={v}" for k, v in sorted(rt_dist.items())),
            "confidence_distribution": conf_dist,
            "amount_variance_total": round(float(np.nansum(variances)), 6) if variances else 0.0,
        }
        pd.DataFrame([summary]).to_csv(self._summary_path, index=False, encoding="utf-8")
        _LOG.info(
            "Reconciliation complete: rows=%d matched=%d unmatched=%d audit=%s",
            n,
            matched,
            unmatched,
            self._audit_path,
        )
        return summary

    def _bucket_confidence(self, scores: list[int]) -> str:
        b100 = sum(1 for s in scores if s == 100)
        b80 = sum(1 for s in scores if 80 <= s < 100)
        b50 = sum(1 for s in scores if 50 <= s < 80)
        blow = sum(1 for s in scores if s < 50)
        return f"100={b100};80-99={b80};50-79={b50};lt50={blow}"

    def _empty_summary(self, batch: str, duplicate_summary: Mapping[str, Any] | None) -> dict[str, Any]:
        ds = duplicate_summary or {}
        return {
            "processing_batch_id": batch,
            "total_rows": 0,
            "total_matched": 0,
            "unmatched": 0,
            "duplicate_exact_pairs": int(ds.get("exact_duplicate_pairs", 0)),
            "duplicate_fuzzy_pairs": int(ds.get("fuzzy_duplicate_pairs", 0)),
            "duplicate_retry_pairs": int(ds.get("retry_pattern_pairs", 0)),
            "duplicate_possible_pairs": int(ds.get("possible_duplicate_pairs", 0)),
            "reconciliation_distribution": "",
            "confidence_distribution": "",
            "amount_variance_total": 0.0,
        }
