"""
ForgeX — CSV & Excel Data Ingestion & Export Engine
===================================================
Robust parsing, column-mapping, validation, and safe export for tabular batch inputs:
  - Supports CSV, TSV, and XLSX (multi-sheet workbooks)
  - Enforces 5 MB size limit and 500 row limit
  - Handles UTF-8 with BOM (utf-8-sig), cp1252, and delimiter sniffing (comma, tab, semicolon)
  - openpyxl with data_only=True so formulas yield evaluated values
  - Preview first 20 rows and interactive column mapping
  - Row-level validation errors
  - CSV/XLSX export with formula injection escaping (cells starting with =, +, -, @)
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
import openpyxl

from app.config import settings

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_ROW_COUNT = 500
PREVIEW_ROW_COUNT = 20

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_formula_cell(val: Any) -> Any:
    """
    Escapes cells starting with formula trigger characters (=, +, -, @)
    by prepending a single quote to prevent spreadsheet formula injection.
    """
    if val is None:
        return ""
    s = str(val)
    if s and s[0] in FORMULA_PREFIXES:
        return f"'{s}"
    return s


class DataIngestService:
    """
    Handles CSV and Excel parsing, preview, mapping validation, and export.
    """

    @staticmethod
    def validate_file_path(path: Path) -> Path:
        resolved = Path(path).resolve()
        # Ensure path is within allowlisted storage directory
        allowed_root = settings.DEFAULT_DATA_DIR.resolve()
        if not resolved.is_relative_to(allowed_root):
            raise PermissionError(
                f"File access outside allowlisted data directory is forbidden: {resolved}"
            )
        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {resolved}")
        size = resolved.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            raise ValueError(
                f"File size ({size / 1024 / 1024:.2f} MB) exceeds maximum limit of 5 MB."
            )
        return resolved

    @classmethod
    def get_sheet_names(cls, file_path: Path) -> List[str]:
        """Returns list of sheet names for Excel workbooks, or ['Sheet1'] for CSV/TSV."""
        resolved = cls.validate_file_path(file_path)
        ext = resolved.suffix.lower()
        if ext in (".xlsx", ".xlsm"):
            wb = openpyxl.load_workbook(resolved, read_only=True, keep_links=False)
            try:
                return wb.sheetnames
            finally:
                wb.close()
        return ["Default"]

    @classmethod
    def _read_csv_raw(cls, path: Path) -> Tuple[List[str], List[List[str]]]:
        """Reads CSV/TSV with encoding detection and delimiter sniffing."""
        raw_bytes = path.read_bytes()

        # Encoding fallback hierarchy: utf-8-sig -> utf-8 -> cp1252
        text = ""
        for enc in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        if not text:
            text = raw_bytes.decode("latin-1", errors="replace")

        # Delimiter sniffing on first 4KB
        sample = text[:4096]
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            delimiter = dialect.delimiter
        except Exception:
            if "\t" in sample:
                delimiter = "\t"
            elif ";" in sample:
                delimiter = ";"

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows: List[List[str]] = []
        for r in reader:
            # Skip completely empty rows
            if any(cell.strip() for cell in r):
                rows.append([cell.strip() for cell in r])

        if not rows:
            return [], []

        headers = [h.strip() for h in rows[0]]
        data_rows = rows[1:]
        return headers, data_rows

    @classmethod
    def _read_excel_raw(
        cls, path: Path, sheet_name: Optional[str] = None
    ) -> Tuple[List[str], List[List[str]]]:
        """Reads XLSX workbook with data_only=True so formulas return evaluated values."""
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            if sheet_name and sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
            else:
                ws = wb.active

            rows: List[List[str]] = []
            for row in ws.iter_rows(values_only=True):
                str_cells = [str(c).strip() if c is not None else "" for c in row]
                if any(str_cells):
                    rows.append(str_cells)

            if not rows:
                return [], []

            headers = [h for h in rows[0]]
            data_rows = rows[1:]
            return headers, data_rows
        finally:
            wb.close()

    @classmethod
    def read_tabular_file(
        cls, file_path: Path, sheet_name: Optional[str] = None
    ) -> Tuple[List[str], List[List[str]]]:
        """Unified reader for CSV, TSV, and XLSX."""
        resolved = cls.validate_file_path(file_path)
        ext = resolved.suffix.lower()
        if ext in (".xlsx", ".xlsm"):
            return cls._read_excel_raw(resolved, sheet_name=sheet_name)
        elif ext in (".csv", ".tsv", ".txt"):
            return cls._read_csv_raw(resolved)
        else:
            raise ValueError(f"Unsupported file format: {ext}. Expected .csv, .tsv, or .xlsx")

    @classmethod
    def preview(
        cls, file_path: Path, sheet_name: Optional[str] = None, max_rows: int = PREVIEW_ROW_COUNT
    ) -> Dict[str, Any]:
        """
        Inspects file, provides headers, total row count, and first 20 sample rows.
        """
        resolved = cls.validate_file_path(file_path)
        sheets = cls.get_sheet_names(resolved)
        headers, data_rows = cls.read_tabular_file(resolved, sheet_name=sheet_name)

        total_rows = len(data_rows)
        sample_slice = data_rows[:max_rows]

        dict_rows = []
        for idx, row in enumerate(sample_slice, start=1):
            row_dict = {}
            for h_idx, h in enumerate(headers):
                row_dict[h] = row[h_idx] if h_idx < len(row) else ""
            dict_rows.append({"_row_number": idx, **row_dict})

        return {
            "file_name": resolved.name,
            "sheet_names": sheets,
            "active_sheet": sheet_name or (sheets[0] if sheets else "Default"),
            "headers": headers,
            "total_rows": total_rows,
            "sample_rows": dict_rows,
        }

    @classmethod
    def parse_and_validate(
        cls,
        file_path: Path,
        column_mapping: Dict[str, str],
        sheet_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Maps source columns to target schema:
          - subject_name (required)
          - work_date (optional)
          - content_type (optional, default 'Hero')
          - notes (optional)
          - extra columns stored in extra_context
        Returns valid_rows and row-level errors.
        """
        resolved = cls.validate_file_path(file_path)
        headers, data_rows = cls.read_tabular_file(resolved, sheet_name=sheet_name)

        if len(data_rows) > MAX_ROW_COUNT:
            raise ValueError(
                f"File contains {len(data_rows)} rows, exceeding maximum limit of {MAX_ROW_COUNT}."
            )

        name_col = column_mapping.get("subject_name")
        if not name_col or name_col not in headers:
            raise ValueError(
                f"Missing mapping for required column 'subject_name'. Mapped: '{name_col}', Headers: {headers}"
            )

        date_col = column_mapping.get("work_date")
        type_col = column_mapping.get("content_type")
        notes_col = column_mapping.get("notes")

        # Invert header to index
        header_to_idx = {h: idx for idx, h in enumerate(headers)}

        valid_rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for row_idx, row in enumerate(data_rows, start=1):
            def get_val(col_name: Optional[str]) -> str:
                if not col_name or col_name not in header_to_idx:
                    return ""
                idx = header_to_idx[col_name]
                return row[idx].strip() if idx < len(row) else ""

            subject = get_val(name_col)
            if not subject:
                errors.append({
                    "row_number": row_idx,
                    "error": "Subject name is blank or missing",
                    "raw_row": row,
                })
                continue

            # Extra columns mapped as context
            extra_context = {}
            for h in headers:
                if h not in (name_col, date_col, type_col, notes_col):
                    idx = header_to_idx[h]
                    val = row[idx].strip() if idx < len(row) else ""
                    if val:
                        extra_context[h] = val

            valid_rows.append({
                "row_number": row_idx,
                "subject_name": subject,
                "work_date": get_val(date_col),
                "content_type": get_val(type_col) or "Hero",
                "notes": get_val(notes_col),
                "extra_context": extra_context,
            })

        return {
            "total_rows": len(data_rows),
            "valid_count": len(valid_rows),
            "error_count": len(errors),
            "valid_rows": valid_rows,
            "errors": errors,
        }

    # ───────────────────────────────────────────────────────────────────────
    # Safe Export (CSV & XLSX with Formula Injection Escaping)
    # ───────────────────────────────────────────────────────────────────────
    @staticmethod
    def export_to_csv(rows: List[Dict[str, Any]], output_path: Path):
        """Exports rows to CSV with formula injection escaping and UTF-8 BOM."""
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not rows:
            output_path.write_text("", encoding="utf-8-sig")
            return

        headers = list(rows[0].keys())
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in rows:
                escaped_row = [escape_formula_cell(r.get(h, "")) for h in headers]
                writer.writerow(escaped_row)

    @staticmethod
    def export_to_xlsx(rows: List[Dict[str, Any]], output_path: Path, sheet_title: str = "ForgeX Results"):
        """Exports rows to XLSX with formula injection escaping."""
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_title

        if rows:
            headers = list(rows[0].keys())
            ws.append(headers)
            for r in rows:
                escaped_row = [escape_formula_cell(r.get(h, "")) for h in headers]
                ws.append(escaped_row)

        wb.save(output_path)
