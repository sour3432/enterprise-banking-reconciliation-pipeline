"""
Duplicate intelligence for validated gold-layer banking extracts.

Detects exact duplicates, fuzzy reference/customer/amount/time similarity,
and operational retry/replay patterns. Emits profiling outputs and scores.
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
from rapidfuzz import fuzz

from utils.logger import get_logger

_LOG = get_logger("duplicate_intelligence")


def _resolve_path(project_root: Path, configured: str | Path) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else (project_root / path)


def discover_gold_csvs(gold_dir: Path, processing_batch_id: str | None = None) -> list[Path]:
    if not gold_dir.is_dir():
        _LOG.warning("Gold directory missing: %s", gold_dir)
        return []
    files = sorted(
        p
        for p in gold_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".csv"
        and not p.name.lower().startswith("gold_")
    )
    if processing_batch_id:
        suffix = f"__{processing_batch_id}.csv"
        files = [p for p in files if p.name.endswith(suffix)]
    _LOG.info("Discovered %d gold CSV file(s) for duplicate pass.", len(files))
    return files


def _first_col(df: pd.DataFrame, names: tuple[str, ...]) -> Optional[pd.Series]:
    for n in names:
        if n in df.columns:
            return df[n]
    return None


def _series_txn_id(df: pd.DataFrame) -> pd.Series:
    s = _first_col(
        df,
        (
            "txn_id_standardized",
            "transaction_id",
            "txn_id",
        ),
    )
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


def _series_timestamp(df: pd.DataFrame) -> pd.Series:
    s = _first_col(
        df,
        (
            "txn_timestamp_parsed",
            "txn_timestamp",
            "ingestion_timestamp",
        ),
    )
    if s is None:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    dt = pd.to_datetime(s, errors="coerce", utc=True)
    return dt


def _series_customer_id(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("customer_id",))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.strip()


def _series_customer_name(df: pd.DataFrame) -> pd.Series:
    s = _first_col(df, ("full_name", "first_name", "customer_email"))
    if s is None:
        return pd.Series("", index=df.index, dtype=object)
    return s.fillna("").astype(str).str.strip()


def _series_reference(df: pd.DataFrame) -> pd.Series:
    parts: list[pd.Series] = []
    for col in (
        "settlement_reference_standardized",
        "settlement_reference",
        "utr_number_standardized",
        "utr_number",
        "reference_number_standardized",
        "reference_number",
        "bank_ref_no_standardized",
        "bank_ref_no",
    ):
        if col in df.columns:
            parts.append(df[col].fillna("").astype(str).str.strip())
    if not parts:
        return pd.Series("", index=df.index, dtype=object)
    acc = parts[0].copy()
    for p in parts[1:]:
        acc = acc.where(acc.ne(""), p)
    return acc.mask(acc.str.lower().isin({"nan", "none"}), "")


def _normalize_id(raw: str) -> str:
    t = str(raw or "").strip()
    if not t or t.lower() in {"nan", "none"}:
        return ""
    t = re.sub(r"[\s\-_]+", "", t, flags=re.UNICODE)
    return t.upper()


@dataclass
class DuplicateRunSummary:
    processing_batch_id: str
    gold_files_processed: int
    total_rows: int
    exact_duplicate_pairs: int
    fuzzy_duplicate_pairs: int
    retry_pattern_pairs: int
    possible_duplicate_pairs: int
    not_duplicate_rows: int


@dataclass
class DuplicateEngine:
    """
    Operational duplicate intelligence over validated gold CSVs.

    Args:
        project_root: Repository root.
        config: Parsed ``config.yaml``.
        processing_batch_id: If set, only gold files named ``*__{id}.csv`` are read.
    """

    project_root: Path
    config: Mapping[str, Any]
    processing_batch_id: str | None = None

    def __post_init__(self) -> None:
        dup_cfg = self.config.get("duplicate_intelligence") or {}
        self._gold_dir = _resolve_path(self.project_root, self.config.get("gold_path", "data/gold"))
        out_base = _resolve_path(self.project_root, self.config.get("output_path", "outputs"))
        self._detail_path = out_base / dup_cfg.get(
            "duplicate_detail_path", "profiling_reports/duplicate_intelligence_detailed.csv"
        )
        self._summary_path = out_base / dup_cfg.get(
            "duplicate_summary_path", "profiling_reports/duplicate_intelligence_summary.csv"
        )
        self._retry_window_sec = int(dup_cfg.get("retry_window_seconds", 300))
        self._fuzzy_time_sec = int(dup_cfg.get("fuzzy_time_window_seconds", 86400))
        self._amount_rel_tol = float(dup_cfg.get("amount_relative_tolerance", 0.005))
        self._max_pairs_per_block = int(dup_cfg.get("max_fuzzy_pairs_per_block", 2000))

    def run(self) -> DuplicateRunSummary:
        self._detail_path.parent.mkdir(parents=True, exist_ok=True)
        self._summary_path.parent.mkdir(parents=True, exist_ok=True)

        batch = self.processing_batch_id
        paths = discover_gold_csvs(self._gold_dir, batch)
        if not paths:
            _LOG.warning("No gold files for duplicate intelligence (batch=%s).", batch)
            summary = DuplicateRunSummary(
                processing_batch_id=batch or "",
                gold_files_processed=0,
                total_rows=0,
                exact_duplicate_pairs=0,
                fuzzy_duplicate_pairs=0,
                retry_pattern_pairs=0,
                possible_duplicate_pairs=0,
                not_duplicate_rows=0,
            )
            pd.DataFrame([vars(summary)]).to_csv(self._summary_path, index=False, encoding="utf-8")
            pd.DataFrame().to_csv(self._detail_path, index=False, encoding="utf-8")
            return summary

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

        all_df = pd.concat(frames, ignore_index=True)
        n = len(all_df)
        tid = _series_txn_id(all_df)
        amt = _series_amount(all_df)
        ts = _series_timestamp(all_df)
        cust = _series_customer_id(all_df)
        name = _series_customer_name(all_df)
        ref = _series_reference(all_df)

        tid_n = tid.map(_normalize_id)
        amt_key = amt.round(6)
        ts_na = ts.isna()
        ts_key = ts.dt.floor("s") if hasattr(ts.dt, "floor") else ts

        exact_key = pd.Series(
            [
                f"{a}|{b}|{c}"
                for a, b, c in zip(
                    tid_n.tolist(),
                    amt_key.replace({np.nan: np.nan}).astype(str).tolist(),
                    ts_key.astype(str).where(~ts_na, "NAT").tolist(),
                    strict=False,
                )
            ],
            index=all_df.index,
        )
        bad_exact = tid_n.eq("") | amt.isna() | ts_na
        exact_key = exact_key.mask(bad_exact, "")

        vc = exact_key.replace("", pd.NA).value_counts()
        dup_exact_mask = exact_key.ne("") & exact_key.map(lambda k: vc.get(k, 0) > 1 if pd.notna(k) else False)

        detail_rows: list[dict[str, Any]] = []
        exact_pairs = 0
        fuzzy_pairs = 0
        retry_pairs = 0
        possible_pairs = 0

        # Exact duplicate pairs (within same exact_key, unique indices)
        _work_exact = all_df.assign(_exact_key=exact_key)
        for key, grp in _work_exact.groupby("_exact_key", sort=False):
            if not key or len(grp) < 2:
                continue
            idxs = grp.index.tolist()
            for ii in range(len(idxs)):
                for jj in range(ii + 1, len(idxs)):
                    i, j = idxs[ii], idxs[jj]
                    exact_pairs += 1
                    detail_rows.append(
                        self._pair_row(
                            all_df,
                            i,
                            j,
                            "EXACT_DUPLICATE",
                            100,
                            "same_transaction_id_amount_timestamp",
                            tid,
                            amt,
                            ts,
                            cust,
                            ref,
                        )
                    )

        # Retry / replay: same normalized amount + same customer + distinct txn + close timestamps
        amt_block = amt.round(2)
        for (cid, ab), grp_idx in all_df.groupby([cust, amt_block], sort=False).indices.items():
            if cid is None or str(cid).strip() == "" or pd.isna(ab):
                continue
            idxs = sorted(grp_idx)
            if len(idxs) < 2:
                continue
            sub_ts = ts.reindex(idxs)
            sub_tid = tid_n.reindex(idxs)
            for ii in range(len(idxs)):
                for jj in range(ii + 1, len(idxs)):
                    i, j = idxs[ii], idxs[jj]
                    if sub_tid.iloc[ii] == sub_tid.iloc[jj] and sub_tid.iloc[ii] != "":
                        continue
                    dti = sub_ts.iloc[ii]
                    dtj = sub_ts.iloc[jj]
                    if pd.isna(dti) or pd.isna(dtj):
                        continue
                    delta = abs((dti - dtj).total_seconds())
                    if delta <= self._retry_window_sec:
                        retry_pairs += 1
                        conf = max(50, int(95 - min(45, delta)))
                        detail_rows.append(
                            self._pair_row(
                                all_df,
                                i,
                                j,
                                "RETRY_PATTERN",
                                conf,
                                f"retry_window_{self._retry_window_sec}s_same_customer_amount",
                                tid,
                                amt,
                                ts,
                                cust,
                                ref,
                            )
                        )

        # Same strong reference, different txn_id (gateway replay / resubmit)
        ref_n = ref.map(_normalize_id)
        for rkey, grp in all_df.assign(_refk=ref_n).groupby("_refk", sort=False):
            if not rkey or len(grp) < 2:
                continue
            idxs = sorted(grp.index.tolist())
            sub_tid = tid_n.reindex(idxs)
            if sub_tid.nunique() < 2:
                continue
            for ii in range(len(idxs)):
                for jj in range(ii + 1, len(idxs)):
                    i, j = idxs[ii], idxs[jj]
                    if sub_tid.iloc[ii] == sub_tid.iloc[jj]:
                        continue
                    retry_pairs += 1
                    detail_rows.append(
                        self._pair_row(
                            all_df,
                            i,
                            j,
                            "RETRY_PATTERN",
                            88,
                            "same_reference_different_transaction_id",
                            tid,
                            amt,
                            ts,
                            cust,
                            ref,
                        )
                    )

        # Fuzzy duplicate blocking: customer + day bucket + rounded amount
        day = ts.dt.floor("D")
        block_keys = list(zip(cust.tolist(), day.astype(str).tolist(), amt.round(2).tolist(), strict=False))
        block_map: dict[tuple[Any, str, Any], list[int]] = {}
        for pos, bk in enumerate(block_keys):
            if str(bk[0]).strip() == "" or bk[1] in {"NaT", "nan"} or pd.isna(bk[2]):
                continue
            block_map.setdefault(bk, []).append(pos)

        for _bk, idxs in block_map.items():
            if len(idxs) < 2:
                continue
            idxs = sorted(idxs)
            pair_count = 0
            for ii in range(len(idxs)):
                if pair_count >= self._max_pairs_per_block:
                    _LOG.debug("Fuzzy pair cap reached for block %s", _bk)
                    break
                for jj in range(ii + 1, len(idxs)):
                    if pair_count >= self._max_pairs_per_block:
                        break
                    i, j = idxs[ii], idxs[jj]
                    if dup_exact_mask.iloc[i] and dup_exact_mask.iloc[j]:
                        continue
                    if tid_n.iloc[i] == tid_n.iloc[j] and tid_n.iloc[i] != "":
                        continue
                    dti, dtj = ts.iloc[i], ts.iloc[j]
                    time_ok = False
                    time_score = 0.0
                    if not pd.isna(dti) and not pd.isna(dtj):
                        delta = abs((dti - dtj).total_seconds())
                        time_ok = delta <= self._fuzzy_time_sec
                        time_score = max(0.0, 100.0 - min(100.0, (delta / self._fuzzy_time_sec) * 100.0))
                    else:
                        time_score = 40.0
                        time_ok = True
                    if not time_ok:
                        continue
                    ai, aj = amt.iloc[i], amt.iloc[j]
                    if pd.isna(ai) or pd.isna(aj):
                        continue
                    denom = max(abs(ai), abs(aj), 1.0)
                    rel_diff = abs(ai - aj) / denom
                    if rel_diff > self._amount_rel_tol * 20:
                        continue
                    amount_score = max(0.0, 100.0 - min(100.0, (rel_diff / max(self._amount_rel_tol, 1e-9)) * 50.0))

                    ni, nj = str(name.iloc[i]), str(name.iloc[j])
                    if ni and nj:
                        cust_score = float(fuzz.token_sort_ratio(ni, nj))
                    elif cust.iloc[i] == cust.iloc[j] and str(cust.iloc[i]).strip() != "":
                        cust_score = 95.0
                    else:
                        cust_score = 35.0

                    ri, rj = str(ref.iloc[i]), str(ref.iloc[j])
                    if ri and rj:
                        ref_score = float(fuzz.ratio(ri, rj))
                    else:
                        ref_score = 50.0

                    confidence = int(
                        round(0.30 * cust_score + 0.25 * time_score + 0.25 * amount_score + 0.20 * ref_score)
                    )
                    if confidence >= 82:
                        cls = "FUZZY_DUPLICATE"
                        fuzzy_pairs += 1
                    elif confidence >= 62:
                        cls = "POSSIBLE_DUPLICATE"
                        possible_pairs += 1
                    else:
                        continue
                    pair_count += 1
                    detail_rows.append(
                        self._pair_row(
                            all_df,
                            i,
                            j,
                            cls,
                            confidence,
                            "rapidfuzz_customer_reference_time_amount",
                            tid,
                            amt,
                            ts,
                            cust,
                            ref,
                        )
                    )

        # Row-level NOT_DUPLICATE count: rows with no exact dup key and no fuzzy/retry edges (approximate)
        touched = set()
        for r in detail_rows:
            touched.add(int(r["row_index_a"]))
            touched.add(int(r["row_index_b"]))
        not_dup = int(n - len(touched)) if n else 0

        if detail_rows:
            pd.DataFrame(detail_rows).to_csv(self._detail_path, index=False, encoding="utf-8")
        else:
            pd.DataFrame(
                columns=[
                    "row_index_a",
                    "row_index_b",
                    "source_file_a",
                    "source_file_b",
                    "transaction_id_a",
                    "transaction_id_b",
                    "customer_id_a",
                    "customer_id_b",
                    "reference_a",
                    "reference_b",
                    "duplicate_classification",
                    "duplicate_confidence",
                    "duplicate_logic",
                    "amount_a",
                    "amount_b",
                    "timestamp_a",
                    "timestamp_b",
                    "analysis_timestamp",
                ]
            ).to_csv(self._detail_path, index=False, encoding="utf-8")

        summary = DuplicateRunSummary(
            processing_batch_id=batch or "",
            gold_files_processed=len(paths),
            total_rows=n,
            exact_duplicate_pairs=exact_pairs,
            fuzzy_duplicate_pairs=fuzzy_pairs,
            retry_pattern_pairs=retry_pairs,
            possible_duplicate_pairs=possible_pairs,
            not_duplicate_rows=max(0, not_dup),
        )
        pd.DataFrame([vars(summary)]).to_csv(self._summary_path, index=False, encoding="utf-8")
        _LOG.info(
            "Duplicate intelligence: rows=%d exact_pairs=%d fuzzy=%d retry=%d possible=%d",
            n,
            exact_pairs,
            fuzzy_pairs,
            retry_pairs,
            possible_pairs,
        )
        return summary

    def _pair_row(
        self,
        df: pd.DataFrame,
        i: int,
        j: int,
        classification: str,
        confidence: int,
        logic: str,
        tid: pd.Series,
        amt: pd.Series,
        ts: pd.Series,
        cust: pd.Series,
        ref: pd.Series,
    ) -> dict[str, Any]:
        return {
            "row_index_a": i,
            "row_index_b": j,
            "source_file_a": df.at[i, "_source_file"],
            "source_file_b": df.at[j, "_source_file"],
            "transaction_id_a": tid.iloc[i],
            "transaction_id_b": tid.iloc[j],
            "customer_id_a": cust.iloc[i],
            "customer_id_b": cust.iloc[j],
            "reference_a": ref.iloc[i],
            "reference_b": ref.iloc[j],
            "duplicate_classification": classification,
            "duplicate_confidence": confidence,
            "duplicate_logic": logic,
            "amount_a": amt.iloc[i],
            "amount_b": amt.iloc[j],
            "timestamp_a": ts.iloc[i].isoformat() if pd.notna(ts.iloc[i]) else "",
            "timestamp_b": ts.iloc[j].isoformat() if pd.notna(ts.iloc[j]) else "",
            "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
        }

