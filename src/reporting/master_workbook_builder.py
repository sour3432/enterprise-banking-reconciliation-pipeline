"""
Master executive workbook — consolidated multinational banking operations control pack.

Produces ``Enterprise_Banking_Operations_Master_Report.xlsx`` with fourteen
operational sheets in a fixed order. Tolerant of empty marts and partial inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from .excel_formatters import (
    EnterpriseWorkbookFormats,
    auto_fit_columns,
    write_banner,
    write_dataframe_table,
    write_kpi_block,
    write_section_header,
)
from .master_workbook_data import MATCH_TYPES, SEVERITIES, MasterWorkbookData
from utils.logger import get_logger

_LOG = get_logger("master_workbook")

SHEET_ORDER = [
    "00_Executive_Summary",
    "01_Operations_Scorecard",
    "02_Reconciliation_Overview",
    "03_Validation_Control_Tower",
    "04_Duplicate_Intelligence",
    "05_FX_and_Value_Variance",
    "06_Exception_and_Breaks_Desk",
    "07_Unresolved_Exposure_Register",
    "08_Entity_Account_Performance",
    "09_Trend_and_Volume_Analytics",
    "10_Audit_and_Lineage",
    "11_Batch_Run_Metadata",
    "12_Definitions_and_Methodology",
    "13_Navigation_and_Read_Me",
]


@dataclass
class MasterExecutiveWorkbookBuilder:
    """Build the consolidated master executive workbook."""

    output_path: Path

    def build(self, data: MasterWorkbookData) -> Path:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        wb = xlsxwriter.Workbook(str(self.output_path), {"strings_to_numbers": True})
        fmt = EnterpriseWorkbookFormats(wb)

        builders = [
            self._sheet_executive_summary,
            self._sheet_operations_scorecard,
            self._sheet_reconciliation_overview,
            self._sheet_validation_control_tower,
            self._sheet_duplicate_intelligence,
            self._sheet_fx_variance,
            self._sheet_exception_desk,
            self._sheet_unresolved_exposure,
            self._sheet_entity_performance,
            self._sheet_trend_analytics,
            self._sheet_audit_lineage,
            self._sheet_batch_metadata,
            self._sheet_definitions,
            self._sheet_navigation,
        ]
        for name, builder in zip(SHEET_ORDER, builders):
            ws = wb.add_worksheet(name)
            ws.hide_gridlines(2)
            builder(ws, fmt, data, wb)

        wb.close()
        _LOG.info("Master executive workbook written: %s", self.output_path)
        return self.output_path

    def _sheet_executive_summary(
        self, ws, fmt: EnterpriseWorkbookFormats, data: MasterWorkbookData, wb
    ) -> None:
        sub = (
            f"Batch {data.processing_batch_id or 'N/A'}  |  "
            f"Generated {data.generated_at}  |  "
            "Global Banking Reconciliation — Operational Command Center"
        )
        row = write_banner(ws, fmt, title="ENTERPRISE BANKING OPERATIONS — EXECUTIVE COMMAND CENTER", subtitle=sub)
        row = write_section_header(ws, fmt, row, "1. OPERATIONAL OUTCOME")
        row = write_kpi_block(
            ws,
            fmt,
            row,
            [
                ("Processing completion rate", data.processing_completion_rate_pct, "pct"),
                ("Rows processed", data.total_rows_processed, "int"),
                ("Gold VALID rows", data.total_valid, "int"),
                ("Pipeline duration (sec)", data.pipeline_duration_seconds, "decimal"),
            ],
        )
        row = write_section_header(ws, fmt, row, "2. FINANCIAL & RECONCILIATION EXPOSURE")
        row = write_kpi_block(
            ws,
            fmt,
            row,
            [
                ("Reconciliation match rate", data.reconciliation_match_rate_pct, "pct"),
                ("Unresolved exposure", data.unresolved_exposure_count, "int"),
                ("Duplicate exposure (pairs)", data.duplicate_exposure_pairs, "int"),
                ("Aged break exposure", data.aged_break_exposure, "int"),
            ],
        )
        row = write_section_header(ws, fmt, row, "3. CONTROL FAILURES & VALIDATION HEALTH")
        row = write_kpi_block(
            ws,
            fmt,
            row,
            [
                ("Critical validation (S4+S5)", data.critical_validation_failures, "int"),
                ("Rejected records", data.total_rejected, "int"),
                ("Warning records", data.total_warning, "int"),
                ("FX tolerance breaches", data.fx_tolerance_breaches, "int"),
            ],
        )

        row = write_section_header(ws, fmt, row, "4. OPERATIONAL RISK — TOP ENTITIES")
        headers = ["Entity", "Reject rate %", "S5 count", "Rows processed", "Risk signal"]
        risk_rows = []
        for e in data.top_risk_entities[:8]:
            signal = "ELEVATED" if e["reject_rate_pct"] > 50 or e["severity_S5"] > 1000 else "WATCH"
            risk_rows.append(
                [
                    e["entity"],
                    e["reject_rate_pct"],
                    e["severity_S5"],
                    e["rows_processed"],
                    signal,
                ]
            )
        if not risk_rows:
            risk_rows = [["—", 0, 0, 0, "NO_DATA"]]
        row = write_dataframe_table(ws, fmt, row, headers, risk_rows, freeze_row=row + 1)

        row = write_section_header(ws, fmt, row, "5. EXECUTIVE COMMENTARY & ALERTS")
        ws.write(row, 0, "Operational alerts", fmt.table_header)
        ws.merge_range(row, 1, row, 7, "", fmt.table_header)
        row += 1
        for i, alert in enumerate(data.operational_alerts[:6]):
            f = fmt.alert_high if "critical" in alert.lower() or "unresolved" in alert.lower() else fmt.alert_ok
            ws.merge_range(row + i, 0, row + i, 7, alert, f)
        row += max(1, len(data.operational_alerts[:6])) + 1

        for i, line in enumerate(data.executive_commentary):
            ws.merge_range(row + i, 0, row + i, 7, line, fmt.commentary)
        row += len(data.executive_commentary) + 1

        # Severity chart
        if any(data.severity_counts.values()):
            chart_row = row + 1
            ws.write_row(chart_row, 0, ["Severity"] + list(SEVERITIES), fmt.table_header)
            ws.write_row(chart_row + 1, 0, ["Count"] + [data.severity_counts.get(s, 0) for s in SEVERITIES], fmt.cell)
            chart = wb.add_chart({"type": "column", "subtype": "stacked"})
            chart.add_series(
                {
                    "name": "Severity distribution",
                    "categories": f"='00_Executive_Summary'!$B${chart_row + 2}:$G${chart_row + 2}",
                    "values": f"='00_Executive_Summary'!$B${chart_row + 3}:$G${chart_row + 3}",
                    "fill": {"color": "#5B6770"},
                }
            )
            chart.set_title({"name": "Validation severity (S0–S5)"})
            chart.set_style(2)
            ws.insert_chart(chart_row + 5, 0, chart, {"x_scale": 1.2, "y_scale": 1.1})

        ws.set_column(0, 0, 32)
        ws.set_column(1, 7, 16)
        ws.freeze_panes(3, 0)

    def _sheet_operations_scorecard(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Operations Scorecard", subtitle="Cross-functional KPI panel")
        kpis = [
            ("Throughput (processed)", data.total_rows_processed, "int"),
            ("Valid throughput", data.total_valid, "int"),
            ("Warnings", data.total_warning, "int"),
            ("Rejections", data.total_rejected, "int"),
            ("Match rate", data.reconciliation_match_rate_pct, "pct"),
            ("Duplicate pairs", data.duplicate_exposure_pairs, "int"),
            ("Unresolved", data.unresolved_exposure_count, "int"),
            ("Duration (s)", data.pipeline_duration_seconds, "decimal"),
        ]
        row = write_kpi_block(ws, fmt, row, kpis, cols_per_row=4)

        headers = ["Control domain", "Metric", "Value", "Target", "Status"]
        targets = [
            ("Data quality", "Processing completion %", data.processing_completion_rate_pct, 95.0),
            ("Reconciliation", "Match rate %", data.reconciliation_match_rate_pct, 98.0),
            ("Duplicates", "Pair exposure", data.duplicate_exposure_pairs, 0),
            ("Validation", "Critical failures (S4+S5)", data.critical_validation_failures, 0),
        ]
        rows = []
        for domain, metric, val, tgt in targets:
            if "rate" in metric.lower() or "%" in metric:
                status = "PASS" if float(val) >= float(tgt) else "BREACH"
                disp_val = f"{val:.2f}%"
            else:
                status = "PASS" if float(val) <= float(tgt) else "BREACH"
                disp_val = f"{val:,}"
            rows.append([domain, metric, disp_val, str(tgt), status])
        row = write_dataframe_table(ws, fmt, row, headers, rows, freeze_row=row + 1)

        # Match type mini-chart data
        mrow = row + 1
        ws.write_row(mrow, 0, ["Match type"] + list(MATCH_TYPES), fmt.table_header)
        ws.write_row(
            mrow + 1,
            0,
            ["Count"] + [data.match_type_counts.get(m, 0) for m in MATCH_TYPES],
            fmt.cell,
        )
        chart = wb.add_chart({"type": "bar"})
        chart.add_series(
            {
                "categories": f"='01_Operations_Scorecard'!$B${mrow + 2}:$G${mrow + 2}",
                "values": f"='01_Operations_Scorecard'!$B${mrow + 3}:$G${mrow + 3}",
                "name": "Reconciliation outcomes",
            }
        )
        chart.set_title({"name": "Reconciliation outcome distribution"})
        ws.insert_chart(mrow + 5, 0, chart)

    def _sheet_reconciliation_overview(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Reconciliation Overview", subtitle="Match taxonomy and confidence")
        headers = ["Match type", "Count", "Matched set", "Notes"]
        matched_set = {"FULL_MATCH", "PARTIAL_MATCH", "DATE_MISMATCH", "AMOUNT_MISMATCH"}
        notes = {
            "FULL_MATCH": "Amount and time within tolerance",
            "PARTIAL_MATCH": "Both dimensions outside tolerance",
            "DATE_MISMATCH": "Amount OK, timing break",
            "AMOUNT_MISMATCH": "Timing OK, amount break",
            "MULTIPLE_MATCHES": "Ambiguous candidates — unresolved",
            "MISSING_SETTLEMENT": "No settlement partner — unresolved",
        }
        rows = [
            [
                m,
                data.match_type_counts.get(m, 0),
                "YES" if m in matched_set else "NO",
                notes.get(m, ""),
            ]
            for m in MATCH_TYPES
        ]
        row = write_dataframe_table(ws, fmt, row, headers, rows, freeze_row=row + 1)

        conf_row = row + 1
        if data.confidence_distribution:
            ws.write(conf_row, 0, "Confidence tier", fmt.section)
            conf_row += 1
            ch = ["Tier", "Count"]
            cr = [[k, v] for k, v in data.confidence_distribution.items()]
            conf_row = write_dataframe_table(ws, fmt, conf_row, ch, cr)
            chart = wb.add_chart({"type": "line"})
            chart.add_series(
                {
                    "categories": f"='02_Reconciliation_Overview'!$A${conf_row - len(cr)}:$A${conf_row - 1}",
                    "values": f"='02_Reconciliation_Overview'!$B${conf_row - len(cr)}:$B${conf_row - 1}",
                    "name": "Confidence distribution",
                }
            )
            chart.set_title({"name": "Reconciliation confidence tiers"})
            ws.insert_chart(conf_row + 1, 3, chart)

        if not data.rec_audit_sample.empty:
            sub = data.rec_audit_sample.head(500)
            cols = [c for c in sub.columns if c in sub.columns][:12]
            detail_row = conf_row + 15 if data.confidence_distribution else row + 12
            ws.write(detail_row, 0, "Reconciliation audit sample (capped)", fmt.section)
            detail_row += 1
            body = [[sub.iloc[i].get(c, "") for c in cols] for i in range(min(200, len(sub)))]
            write_dataframe_table(ws, fmt, detail_row, cols, body, freeze_row=detail_row + 1)

    def _sheet_validation_control_tower(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Validation Control Tower", subtitle="S0–S5 severity framework")
        headers = ["Severity", "Count", "Level", "Control response"]
        levels = {
            "S0": "INFO",
            "S1": "LOW",
            "S2": "MEDIUM",
            "S3": "HIGH",
            "S4": "CRITICAL",
            "S5": "FATAL",
        }
        responses = {
            "S0": "Log only",
            "S1": "Monitor",
            "S2": "Source feedback",
            "S3": "Ops review",
            "S4": "Reject / escalate",
            "S5": "Hard stop — mandatory field breach",
        }
        rows = [
            [s, data.severity_counts.get(s, 0), levels[s], responses[s]] for s in SEVERITIES
        ]
        row = write_dataframe_table(ws, fmt, row, headers, rows, freeze_row=row + 1)

        chart = wb.add_chart({"type": "column", "subtype": "stacked"})
        chart.add_series(
            {
                "categories": f"='03_Validation_Control_Tower'!$A$4:$A$9",
                "values": f"='03_Validation_Control_Tower'!$B$4:$B$9",
                "name": "Severity",
                "points": [
                    {"fill": {"color": "#8FAADC"}},
                    {"fill": {"color": "#8FAADC"}},
                    {"fill": {"color": "#F4D58D"}},
                    {"fill": {"color": "#F4D58D"}},
                    {"fill": {"color": "#E2A03F"}},
                    {"fill": {"color": "#C55A5A"}},
                ],
            }
        )
        chart.set_title({"name": "Severity distribution"})
        ws.insert_chart(row + 1, 4, chart)

        if data.validation_rule_failures:
            rrow = row + 18
            ws.write(rrow, 0, "Top validation rule failures (audit sample)", fmt.section)
            rrow += 1
            rule_rows = [[r, c] for r, c in data.validation_rule_failures]
            write_dataframe_table(ws, fmt, rrow, ["Rule", "Count"], rule_rows, freeze_row=rrow + 1)
            chart2 = wb.add_chart({"type": "bar"})
            n = min(10, len(rule_rows))
            chart2.add_series(
                {
                    "categories": f"='03_Validation_Control_Tower'!$A${rrow + 2}:$A${rrow + 1 + n}",
                    "values": f"='03_Validation_Control_Tower'!$B${rrow + 2}:$B${rrow + 1 + n}",
                    "name": "Rule failures",
                }
            )
            chart2.set_title({"name": "Top validation rules"})
            ws.insert_chart(rrow, 4, chart2)

    def _sheet_duplicate_intelligence(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Duplicate Intelligence", subtitle="Clusters, confidence, and source contribution")
        row = write_kpi_block(
            ws,
            fmt,
            row,
            [
                ("Total pairs", data.duplicate_exposure_pairs, "int"),
                ("EXACT", data.duplicate_by_classification.get("EXACT_DUPLICATE", 0), "int"),
                ("FUZZY", data.duplicate_by_classification.get("FUZZY_DUPLICATE", 0), "int"),
                ("RETRY", data.duplicate_by_classification.get("RETRY_PATTERN", 0), "int"),
            ],
        )
        cls_rows = [[k, v] for k, v in sorted(data.duplicate_by_classification.items(), key=lambda x: -x[1])]
        if not cls_rows:
            cls_rows = [["NO_DUPLICATES_DETECTED", 0]]
        row = write_dataframe_table(ws, fmt, row, ["Classification", "Pair count"], cls_rows, freeze_row=row + 1)

        tier_rows = [[k, v] for k, v in data.duplicate_confidence_tiers.items()] or [["No tier data", 0]]
        trow = row + 1
        write_dataframe_table(ws, fmt, trow, ["Confidence tier", "Pairs"], tier_rows)

        if data.duplicate_source_contribution:
            srow = trow + len(tier_rows) + 4
            src_rows = [[e, c] for e, c in data.duplicate_source_contribution]
            write_dataframe_table(ws, fmt, srow, ["Source entity", "Contributions"], src_rows)
            chart = wb.add_chart({"type": "bar"})
            n = min(10, len(src_rows))
            chart.add_series(
                {
                    "categories": f"='04_Duplicate_Intelligence'!$A${srow + 2}:$A${srow + 1 + n}",
                    "values": f"='04_Duplicate_Intelligence'!$B${srow + 2}:$B${srow + 1 + n}",
                }
            )
            chart.set_title({"name": "Duplicate source concentration"})
            ws.insert_chart(srow, 3, chart)

    def _sheet_fx_variance(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="FX and Value Variance", subtitle="Explained vs unexplained variance")
        row = write_kpi_block(
            ws,
            fmt,
            row,
            [
                ("FX rows in mart", len(data.fx_exposure_rows), "int"),
                ("Tolerance breaches", data.fx_tolerance_breaches, "int"),
                ("Currency pairs", len(data.currency_concentration), "int"),
                ("Base currency", "USD", "text"),
            ],
        )
        if data.fx_exposure_rows:
            headers = list(data.fx_exposure_rows[0].keys())
            body = [[r.get(h, "") for h in headers] for r in data.fx_exposure_rows[:300]]
            row = write_dataframe_table(ws, fmt, row, headers, body, freeze_row=row + 1)
        else:
            ws.write(row, 0, "No FX variance mart rows for current batch.", fmt.commentary)
            row += 2

        if data.currency_concentration:
            crow = row + 1
            conc = [[p, c] for p, c in data.currency_concentration]
            write_dataframe_table(ws, fmt, crow, ["Currency pair", "Row count"], conc)

    def _sheet_exception_desk(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Exception and Breaks Desk", subtitle="Actionable operational register")
        headers = [
            "Exception ID",
            "Severity",
            "Category",
            "Status",
            "Owner",
            "Aging",
            "Required action",
            "Confidence",
        ]
        body = [
            [
                e.get("exception_id", ""),
                e.get("severity", ""),
                e.get("category", ""),
                e.get("status", ""),
                e.get("owner", ""),
                e.get("aging_bucket", ""),
                e.get("required_action", ""),
                e.get("confidence", ""),
            ]
            for e in data.exception_register[:2000]
        ]
        if not body:
            body = [
                [
                    "N/A",
                    "—",
                    "—",
                    "NO_EXCEPTIONS",
                    "—",
                    "—",
                    "No reconciliation breaks in audit sample.",
                    "",
                ]
            ]
        row = write_dataframe_table(
            ws,
            fmt,
            row,
            headers,
            body,
            freeze_row=row + 1,
            col_widths={0: 22, 6: 48},
        )
        sev_col = 1
        last = row + len(body) - 1
        if body:
            col_letter = xl_col_to_name(sev_col)
            ws.conditional_format(
                f"{col_letter}{row}:{col_letter}{last}",
                {"type": "text", "criteria": "containing", "value": "S5", "format": fmt.sev_s5},
            )
            ws.conditional_format(
                f"{col_letter}{row}:{col_letter}{last}",
                {"type": "text", "criteria": "containing", "value": "S4", "format": fmt.sev_s4},
            )

    def _sheet_unresolved_exposure(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Unresolved Exposure Register", subtitle="Concentration and aging")
        row = write_kpi_block(
            ws,
            fmt,
            row,
            [
                ("Unresolved count", data.unresolved_exposure_count, "int"),
                ("Aged breaks", data.aged_break_exposure, "int"),
                ("Open register items", len(data.unresolved_register), "int"),
                ("Match rate", data.reconciliation_match_rate_pct, "pct"),
            ],
        )
        aging_rows = [[k, v] for k, v in data.aging_buckets.items()]
        row = write_dataframe_table(ws, fmt, row, ["Aging bucket", "Exposure count"], aging_rows, freeze_row=row + 1)

        chart = wb.add_chart({"type": "column"})
        n = len(aging_rows)
        if n:
            chart.add_series(
                {
                    "categories": f"='07_Unresolved_Exposure_Register'!$A$12:$A${11 + n}",
                    "values": f"='07_Unresolved_Exposure_Register'!$B$12:$B${11 + n}",
                    "name": "Aging distribution",
                }
            )
            chart.set_title({"name": "Unresolved exposure aging"})
            ws.insert_chart(row + 2, 3, chart)

        if data.unresolved_register:
            urow = row + 12
            headers = ["Exception ID", "Severity", "Category", "Owner", "Aging", "Action"]
            body = [
                [
                    e.get("exception_id", ""),
                    e.get("severity", ""),
                    e.get("category", ""),
                    e.get("owner", ""),
                    e.get("aging_bucket", ""),
                    e.get("required_action", ""),
                ]
                for e in data.unresolved_register[:500]
            ]
            write_dataframe_table(ws, fmt, urow, headers, body, freeze_row=urow + 1)

    def _sheet_entity_performance(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Entity & Account Performance", subtitle="Source-level control metrics")
        headers = [
            "Entity",
            "Processed",
            "Valid",
            "Warnings",
            "Rejected",
            "Valid %",
            "Reject %",
            "S5",
            "S3",
            "Dup txn",
        ]
        body = [
            [
                e["entity"],
                e["rows_processed"],
                e["rows_valid"],
                e["rows_warning"],
                e["rows_rejected"],
                e["valid_rate_pct"],
                e["reject_rate_pct"],
                e["severity_S5"],
                e["severity_S3"],
                e["duplicate_txn_count"],
            ]
            for e in data.entity_performance[:100]
        ]
        if not body:
            body = [["NO_ENTITY_DATA", 0, 0, 0, 0, 0, 0, 0, 0, 0]]
        row = write_dataframe_table(ws, fmt, row, headers, body, freeze_row=row + 1)

        if len(body) > 1:
            chart = wb.add_chart({"type": "bar"})
            n = min(12, len(body))
            chart.add_series(
                {
                    "categories": f"='08_Entity_Account_Performance'!$A$4:$A${3 + n}",
                    "values": f"='08_Entity_Account_Performance'!$G$4:$G${3 + n}",
                    "name": "Reject rate %",
                }
            )
            chart.set_title({"name": "Entity reject rate ranking"})
            ws.insert_chart(row + 1, 0, chart, {"x_scale": 1.3})

    def _sheet_trend_analytics(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Trend and Volume Analytics", subtitle="Throughput and validation volume")
        prof = data.val_summary_profiling
        if prof.empty or "source_file" not in prof.columns:
            ws.write(row, 0, "Validation profiling unavailable — run pipeline to populate trends.", fmt.commentary)
            return

        df = prof.head(40)
        headers = ["Source", "Processed", "Rejected", "Warnings", "Valid"]
        body = []
        for _, r in df.iterrows():
            body.append(
                [
                    _short_name(str(r.get("source_file", ""))),
                    _safe_num(r.get("rows_processed")),
                    _safe_num(r.get("rows_rejected")),
                    _safe_num(r.get("rows_warning")),
                    _safe_num(r.get("rows_valid")),
                ]
            )
        row = write_dataframe_table(ws, fmt, row, headers, body, freeze_row=row + 1)

        if len(body) >= 2:
            chart = wb.add_chart({"type": "line"})
            n = len(body)
            chart.add_series(
                {
                    "name": "Processed",
                    "categories": f"='09_Trend_and_Volume_Analytics'!$A$4:$A${3 + n}",
                    "values": f"='09_Trend_and_Volume_Analytics'!$B$4:$B${3 + n}",
                }
            )
            chart.add_series(
                {
                    "name": "Rejected",
                    "categories": f"='09_Trend_and_Volume_Analytics'!$A$4:$A${3 + n}",
                    "values": f"='09_Trend_and_Volume_Analytics'!$C$4:$C${3 + n}",
                }
            )
            chart.set_title({"name": "Volume trend by source (sample)"})
            ws.insert_chart(row + 1, 0, chart, {"x_scale": 1.4})

    def _sheet_audit_lineage(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Audit and Lineage", subtitle="Pipeline traceability")
        headers = ["Stage", "Artifact", "Row count", "Status"]
        body = [[s["stage"], s["artifact"], s["row_count"], s["status"]] for s in data.lineage_stages]
        row = write_dataframe_table(ws, fmt, row, headers, body, freeze_row=row + 1)

        trace_row = row + 2
        ws.write(trace_row, 0, "Traceability metrics", fmt.section)
        trace_row += 1
        metrics = [
            ("Validation audit sample rows", len(data.val_audit_sample)),
            ("Reconciliation audit sample rows", len(data.rec_audit_sample)),
            ("Standardization audit sample rows", len(data.std_audit_sample)),
            ("Transaction master rows", len(data.txn_master)),
        ]
        write_dataframe_table(
            ws,
            fmt,
            trace_row,
            ["Metric", "Value"],
            [[a, b] for a, b in metrics],
        )

    def _sheet_batch_metadata(self, ws, fmt, data: MasterWorkbookData, wb) -> None:
        row = write_banner(ws, fmt, title="Batch Run Metadata", subtitle="Processing timestamps and rerun indicators")
        write_dataframe_table(
            ws,
            fmt,
            row,
            ["Field", "Value"],
            list(data.batch_metadata),
            freeze_row=row + 1,
            col_widths={0: 32, 1: 56},
        )

    def _sheet_definitions(self, ws, fmt, data: MasterWorkbookData, _wb) -> None:
        row = write_banner(ws, fmt, title="Definitions and Methodology", subtitle="Control framework reference")
        definitions = [
            ("S0–S5", "Enterprise severity ladder: S5 fatal mandatory breach through S0 informational."),
            ("Processing completion rate", "VALID gold rows ÷ total rows processed in validation mart."),
            ("Reconciliation match rate", "Matched outcomes ÷ total reconciliation population."),
            ("FULL_MATCH", "Amount and temporal alignment within configured tolerances."),
            ("PARTIAL_MATCH", "Match attempted; both amount and time outside tolerance."),
            ("DATE_MISMATCH / AMOUNT_MISMATCH", "Single-dimension break with counterpart resolved."),
            ("MISSING_SETTLEMENT", "No resolvable settlement reference — unresolved exposure."),
            ("MULTIPLE_MATCHES", "Ambiguous candidate set — requires manual adjudication."),
            ("Duplicate intelligence", "EXACT, FUZZY, RETRY_PATTERN, POSSIBLE_DUPLICATE classifications."),
            ("FX variance", "Treasury vs transaction rate comparison; breach when |variance| > 2%."),
            ("Aging buckets", "Operational SLA bands for open breaks (0–1, 2–7, 8–30, 31+ days)."),
            ("Audit sample cap", "Large audit logs sampled to configured row cap for workbook performance."),
        ]
        for term, desc in definitions:
            ws.write(row, 0, term, fmt.table_header)
            ws.merge_range(row, 1, row, 6, desc, fmt.commentary)
            row += 1
        auto_fit_columns(ws, 7, [28, 72])

    def _sheet_navigation(self, ws, fmt, data: MasterWorkbookData, _wb) -> None:
        row = write_banner(
            ws,
            fmt,
            title="Navigation and Read Me",
            subtitle="Enterprise Banking Operations Master Report",
        )
        ws.merge_range(
            row,
            0,
            row + 2,
            7,
            "This workbook consolidates gold marts, validation and reconciliation audits, "
            "duplicate intelligence, and profiling outputs into a single executive control pack. "
            "Begin at 00_Executive_Summary for operational posture, then drill into domain sheets. "
            "All figures reflect the latest pipeline batch unless noted.",
            fmt.commentary,
        )
        row += 4
        nav = [
            ("00_Executive_Summary", "Command center — KPIs, risk entities, alerts"),
            ("01_Operations_Scorecard", "Cross-domain scorecard vs targets"),
            ("02_Reconciliation_Overview", "Match taxonomy and confidence"),
            ("03_Validation_Control_Tower", "S0–S5 severity and rule failures"),
            ("04_Duplicate_Intelligence", "Clusters and source contribution"),
            ("05_FX_and_Value_Variance", "FX exposure and tolerance breaches"),
            ("06_Exception_and_Breaks_Desk", "Actionable exception register"),
            ("07_Unresolved_Exposure_Register", "Aging and open exposure"),
            ("08_Entity_Account_Performance", "Entity-level throughput and rejects"),
            ("09_Trend_and_Volume_Analytics", "Volume trends by source"),
            ("10_Audit_and_Lineage", "Stage lineage and traceability"),
            ("11_Batch_Run_Metadata", "Batch IDs and run timestamps"),
            ("12_Definitions_and_Methodology", "Terminology reference"),
        ]
        write_dataframe_table(ws, fmt, row, ["Sheet", "Purpose"], nav, freeze_row=row + 1)
        ws.write(row + len(nav) + 2, 0, f"Batch: {data.processing_batch_id}", fmt.subtitle)


def _short_name(path: str) -> str:
    name = Path(path.replace("\\", "/")).name
    return name[:48] + "…" if len(name) > 48 else name


def _safe_num(v: object) -> float:
    try:
        return float(str(v).strip() or 0)
    except ValueError:
        return 0.0


def build_master_executive_workbook(
    *,
    project_root: Path,
    config: Mapping[str, Any],
    processing_batch_id: str | None,
    pipeline_duration_seconds: float,
    output_path: Path | None = None,
) -> Path:
    """Load consolidated data and write the master workbook."""
    rcfg = config.get("reporting") or {}
    out_base = Path(config.get("output_path", "outputs"))
    if not out_base.is_absolute():
        out_base = project_root / out_base
    excel_dir = out_base / rcfg.get("excel_output_dir", "excel_reports")
    target = output_path or excel_dir / rcfg.get(
        "master_executive_workbook",
        "Enterprise_Banking_Operations_Master_Report.xlsx",
    )
    if not target.is_absolute():
        target = project_root / target

    data = MasterWorkbookData.load(
        project_root=project_root,
        config=config,
        processing_batch_id=processing_batch_id,
        pipeline_duration_seconds=pipeline_duration_seconds,
    )
    return MasterExecutiveWorkbookBuilder(output_path=target).build(data)
