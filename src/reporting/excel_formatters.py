"""
Reusable enterprise Excel formatting for operational banking workbooks.

Uses xlsxwriter format objects with a restrained navy / charcoal palette.
"""

from __future__ import annotations

from dataclasses import dataclass

import xlsxwriter
from xlsxwriter.workbook import Workbook
from xlsxwriter.worksheet import Worksheet


@dataclass(frozen=True)
class Palette:
    navy: str = "#1B2A41"
    charcoal: str = "#2F3E4E"
    header_fill: str = "#D6DCE4"
    alt_row: str = "#F4F6F8"
    white: str = "#FFFFFF"
    risk_critical: str = "#C55A5A"
    risk_high: str = "#E2A03F"
    risk_medium: str = "#F4D58D"
    risk_low: str = "#8FAADC"
    positive: str = "#5A8F6B"
    muted_text: str = "#5B6770"
    kpi_border: str = "#B4BEC8"


class EnterpriseWorkbookFormats:
    """Centralized xlsxwriter formats for master executive workbooks."""

    def __init__(self, wb: Workbook) -> None:
        self.wb = wb
        self.p = Palette()

        self.title = wb.add_format(
            {
                "bold": True,
                "font_size": 16,
                "font_color": self.p.white,
                "bg_color": self.p.navy,
                "align": "left",
                "valign": "vcenter",
            }
        )
        self.subtitle = wb.add_format(
            {
                "italic": True,
                "font_size": 10,
                "font_color": self.p.muted_text,
                "align": "left",
            }
        )
        self.section = wb.add_format(
            {
                "bold": True,
                "font_size": 11,
                "font_color": self.p.white,
                "bg_color": self.p.charcoal,
                "align": "left",
                "valign": "vcenter",
            }
        )
        self.table_header = wb.add_format(
            {
                "bold": True,
                "font_color": self.p.navy,
                "bg_color": self.p.header_fill,
                "border": 1,
                "border_color": self.p.kpi_border,
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
            }
        )
        self.cell = wb.add_format(
            {"border": 1, "border_color": self.p.kpi_border, "valign": "vcenter"}
        )
        self.cell_alt = wb.add_format(
            {
                "border": 1,
                "border_color": self.p.kpi_border,
                "bg_color": self.p.alt_row,
                "valign": "vcenter",
            }
        )
        self.cell_center = wb.add_format(
            {
                "border": 1,
                "border_color": self.p.kpi_border,
                "align": "center",
                "valign": "vcenter",
            }
        )
        self.kpi_label = wb.add_format(
            {
                "bold": True,
                "font_size": 9,
                "font_color": self.p.muted_text,
                "align": "left",
                "valign": "vcenter",
            }
        )
        self.kpi_value = wb.add_format(
            {
                "bold": True,
                "font_size": 14,
                "font_color": self.p.navy,
                "align": "right",
                "valign": "vcenter",
                "num_format": "#,##0",
            }
        )
        self.kpi_value_pct = wb.add_format(
            {
                "bold": True,
                "font_size": 14,
                "font_color": self.p.navy,
                "align": "right",
                "valign": "vcenter",
                "num_format": "0.00%",
            }
        )
        self.kpi_value_decimal = wb.add_format(
            {
                "bold": True,
                "font_size": 14,
                "font_color": self.p.navy,
                "align": "right",
                "valign": "vcenter",
                "num_format": "#,##0.00",
            }
        )
        self.commentary = wb.add_format(
            {
                "text_wrap": True,
                "font_size": 10,
                "font_color": self.p.charcoal,
                "valign": "top",
                "border": 1,
                "border_color": self.p.kpi_border,
            }
        )
        self.link = wb.add_format(
            {
                "font_color": "#0563C1",
                "underline": 1,
                "valign": "vcenter",
            }
        )
        self.sev_s5 = wb.add_format(
            {
                "bg_color": self.p.risk_critical,
                "font_color": self.p.white,
                "bold": True,
                "border": 1,
                "align": "center",
            }
        )
        self.sev_s4 = wb.add_format(
            {
                "bg_color": self.p.risk_high,
                "font_color": self.p.white,
                "bold": True,
                "border": 1,
                "align": "center",
            }
        )
        self.sev_s3 = wb.add_format(
            {
                "bg_color": self.p.risk_medium,
                "border": 1,
                "align": "center",
            }
        )
        self.alert_high = wb.add_format(
            {"bg_color": "#FCE4D6", "border": 1, "text_wrap": True, "valign": "vcenter"}
        )
        self.alert_ok = wb.add_format(
            {"bg_color": "#E2EFDA", "border": 1, "text_wrap": True, "valign": "vcenter"}
        )

    def severity_format(self, severity: str) -> xlsxwriter.format.Format:
        s = str(severity or "").strip().upper()
        if s == "S5":
            return self.sev_s5
        if s == "S4":
            return self.sev_s4
        if s in {"S3", "S2", "S1"}:
            return self.sev_s3
        return self.cell_center


def write_banner(
    ws: Worksheet,
    fmt: EnterpriseWorkbookFormats,
    *,
    title: str,
    subtitle: str = "",
    merge_cols: int = 8,
) -> int:
    """Write title band; returns next data row index."""
    last_col = max(merge_cols - 1, 0)
    ws.merge_range(0, 0, 0, last_col, title, fmt.title)
    if subtitle:
        ws.merge_range(1, 0, 1, last_col, subtitle, fmt.subtitle)
        return 3
    return 2


def write_section_header(ws: Worksheet, fmt: EnterpriseWorkbookFormats, row: int, label: str, width: int = 8) -> int:
    ws.merge_range(row, 0, row, width - 1, label, fmt.section)
    return row + 1


def write_kpi_block(
    ws: Worksheet,
    fmt: EnterpriseWorkbookFormats,
    row: int,
    kpis: list[tuple[str, object, str]],
    *,
    cols_per_row: int = 4,
) -> int:
    """
    Write KPI tiles. ``kind`` is ``int`` | ``pct`` | ``text`` | ``decimal``.
    Returns row after the block.
    """
    col = 0
    start_row = row
    for label, value, kind in kpis:
        if col >= cols_per_row:
            col = 0
            row += 3
        base_col = col * 3
        ws.write(row, base_col, label, fmt.kpi_label)
        if kind == "pct":
            try:
                ws.write_number(row, base_col + 1, float(value) / 100.0, fmt.kpi_value_pct)
            except (TypeError, ValueError):
                ws.write(row, base_col + 1, str(value), fmt.cell)
        elif kind in {"int", "decimal"}:
            try:
                ffmt = fmt.kpi_value if kind == "int" else fmt.kpi_value_decimal
                ws.write_number(row, base_col + 1, float(value), ffmt)
            except (TypeError, ValueError):
                ws.write(row, base_col + 1, str(value), fmt.cell)
        else:
            ws.write(row, base_col + 1, str(value), fmt.cell)
        col += 1
    return max(row, start_row) + 3


def write_dataframe_table(
    ws: Worksheet,
    fmt: EnterpriseWorkbookFormats,
    row: int,
    headers: list[str],
    rows: list[list[object]],
    *,
    freeze_row: int | None = None,
    autofilter: bool = True,
    col_widths: dict[int, int] | None = None,
) -> int:
    """Write header + body; return next free row."""
    for c, h in enumerate(headers):
        ws.write(row, c, h, fmt.table_header)
    body_start = row + 1
    for i, data_row in enumerate(rows):
        f = fmt.cell_alt if i % 2 else fmt.cell
        for c, val in enumerate(data_row):
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                ws.write_number(body_start + i, c, float(val), f)
            else:
                ws.write(body_start + i, c, str(val) if val is not None else "", f)
    last_row = body_start + len(rows) - 1 if rows else row
    if autofilter and headers:
        ws.autofilter(row, 0, max(row, last_row), len(headers) - 1)
    if freeze_row is not None:
        ws.freeze_panes(freeze_row, 0)
    if col_widths:
        for c, w in col_widths.items():
            ws.set_column(c, c, w)
    else:
        for c, h in enumerate(headers):
            ws.set_column(c, c, min(42, max(10, len(str(h)) + 4)))
    return last_row + 2


def auto_fit_columns(ws: Worksheet, col_count: int, widths: list[int] | None = None) -> None:
    if widths:
        for i, w in enumerate(widths[:col_count]):
            ws.set_column(i, i, w)
    else:
        for i in range(col_count):
            ws.set_column(i, i, 14)
