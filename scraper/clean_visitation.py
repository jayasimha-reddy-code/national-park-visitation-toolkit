#!/usr/bin/env python3
"""Clean the official NPS Visitor Use Statistics ``Main_Data.csv`` export.

Input schema (official package, 1979-present)::

    UnitCode,Year,Month,Statistic,Value

Outputs:
  * cleaned CSV with normalized columns, a derived first-of-month date, a
    human-readable statistic label, and the numeric value;
  * a concise Markdown data-quality report;
  * a CSV listing every dropped/flagged source row and the reason.

Rows with missing values are not silently converted to zero.  Duplicate keys
(unit, year, month, statistic) are resolved explicitly: the first valid row is
kept, exact duplicates are counted, and conflicting duplicates are reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

LOG = logging.getLogger("clean_visitation")

REQUIRED_COLUMNS = ["unit_code", "year", "month", "statistic", "value"]
COLUMN_ALIASES = {
    "unitcode": "unit_code",
    "unit": "unit_code",
    "park_code": "unit_code",
    "parkcode": "unit_code",
    "year": "year",
    "month": "month",
    "statistic": "statistic",
    "stat": "statistic",
    "value": "value",
    "values": "value",
    "visit_count": "value",
    "visits": "value",
}
STATISTIC_LABELS = {
    "BC": "Backcountry Overnight Stays",
    "CCG": "Concessionaire Operated Campground Overnight Stays",
    "CL": "Concessionaire Operated Lodging Overnight Stays",
    "MISC": "Miscellaneous Overnight Stays",
    "NROS": "Non-Recreation Overnight Stays",
    "TH": "Visitor Hours (Recreation plus Non-Recreation)",
    "TNRV": "Non-Recreation Visits",
    "TNRVH": "Non-Recreation Visitor Hours",
    "TOS": "Total Overnight Stays",
    "TRV": "Recreation Visits",
    "TRVH": "Recreation Visitor Hours",
    "TRVS": "NPS Operated Campground RV Overnight Stays",
    "TT": "NPS Operated Campground Tent Overnight Stays",
    "TTRV": "NPS Operated Campground Overnight Stays (Tent plus RV)",
    "TV": "Total Visits (Recreation plus Non-Recreation)",
}
MIN_YEAR = 1900
MAX_YEAR = 2100
MAX_VALUE = 1_000_000_000_000
CLEANED_COLUMNS = [
    "unit_code",
    "year",
    "month",
    "date",
    "statistic",
    "statistic_label",
    "value",
]
ISSUE_COLUMNS = [
    "source_row",
    "issue",
    "detail",
    "unit_code",
    "year",
    "month",
    "statistic",
    "value",
]


class DataQualityError(RuntimeError):
    """Input schema or content cannot be processed."""


@dataclass
class QualityStats:
    input_path: str
    output_path: str
    rows_read: int = 0
    rows_written: int = 0
    rows_filtered_by_statistic: int = 0
    columns_original: list[str] = field(default_factory=list)
    columns_normalized: dict[str, str] = field(default_factory=dict)
    columns_dropped: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    invalid_unit: int = 0
    invalid_statistic: int = 0
    invalid_year: int = 0
    invalid_month: int = 0
    missing_value: int = 0
    invalid_numeric: int = 0
    negative_value: int = 0
    out_of_range_value: int = 0
    duplicate_keys: int = 0
    duplicate_exact_rows: int = 0
    duplicate_conflict_rows: int = 0
    unknown_statistics: int = 0
    unit_count: int = 0
    date_min: str | None = None
    date_max: str | None = None
    rows_by_statistic: dict[str, int] = field(default_factory=dict)
    issues_total: int = 0
    value_dtype: str = "unknown"
    source_metadata: dict[str, Any] | None = None
    output_sha256: str | None = None
    input_sha256: str | None = None
    issues_path: str | None = None


def normalize_column_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", str(name).strip()).strip("_").lower()
    return COLUMN_ALIASES.get(cleaned, cleaned)


def normalize_columns(columns: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    normalized: list[str] = []
    mapping: dict[str, str] = {}
    for original in columns:
        new = normalize_column_name(original)
        if new in normalized:
            raise DataQualityError(
                f"column normalization produced a duplicate name {new!r}; "
                "rename the input columns unambiguously"
            )
        normalized.append(new)
        if new != str(original):
            mapping[str(original)] = new
    return normalized, mapping


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue_rows(
    df: pd.DataFrame, mask: pd.Series, issue: str, detail: str = ""
) -> list[dict[str, Any]]:
    if not mask.any():
        return []
    subset = df.loc[mask]
    records: list[dict[str, Any]] = []
    for _, row in subset.iterrows():
        records.append(
            {
                "source_row": int(row["_source_row"]),
                "issue": issue,
                "detail": detail,
                "unit_code": row.get("unit_code", ""),
                "year": row.get("year", ""),
                "month": row.get("month", ""),
                "statistic": row.get("statistic", ""),
                "value": row.get("value", ""),
            }
        )
    return records


def clean_dataframe(
    raw: pd.DataFrame, *, statistics: set[str] | None = None
) -> tuple[pd.DataFrame, list[dict[str, Any]], QualityStats]:
    """Normalize and validate an already-read raw dataframe."""
    stats = QualityStats(input_path="", output_path="")
    stats.rows_read = len(raw)
    stats.columns_original = [str(column) for column in raw.columns]

    normalized, mapping = normalize_columns(raw.columns)
    stats.columns_normalized = mapping
    raw = raw.copy()
    raw.columns = normalized

    missing = [column for column in REQUIRED_COLUMNS if column not in raw.columns]
    if missing:
        stats.missing_columns = missing
        raise DataQualityError(
            "input is missing required column(s) after normalization: "
            + ", ".join(missing)
        )
    stats.columns_dropped = [c for c in normalized if c not in REQUIRED_COLUMNS]
    raw = raw[REQUIRED_COLUMNS].copy()
    raw.insert(0, "_source_row", range(2, len(raw) + 2))

    for column in ("unit_code", "statistic"):
        raw[column] = raw[column].astype("string").str.strip()
    raw["unit_code"] = raw["unit_code"].str.upper()
    raw["statistic"] = raw["statistic"].str.upper()

    issues: list[dict[str, Any]] = []

    if statistics is not None:
        before = len(raw)
        raw = raw[raw["statistic"].isin(statistics)].copy()
        stats.rows_filtered_by_statistic = before - len(raw)
        LOG.info(
            "Filtered to %s; excluded %d rows of other statistics",
            ", ".join(sorted(statistics)),
            stats.rows_filtered_by_statistic,
        )

    unit_missing = raw["unit_code"].isna() | (raw["unit_code"] == "")
    issues += _issue_rows(raw, unit_missing, "missing_unit_code")
    stats.invalid_unit = int(unit_missing.sum())
    raw = raw.loc[~unit_missing].copy()

    stat_missing = raw["statistic"].isna() | (raw["statistic"] == "")
    issues += _issue_rows(raw, stat_missing, "missing_statistic")
    stats.invalid_statistic = int(stat_missing.sum())
    raw = raw.loc[~stat_missing].copy()

    raw["_year"] = pd.to_numeric(raw["year"], errors="coerce")
    raw["_month"] = pd.to_numeric(raw["month"], errors="coerce")
    invalid_year = raw["_year"].isna() | (raw["_year"] % 1 != 0)
    invalid_month = raw["_month"].isna() | (raw["_month"] % 1 != 0)
    issues += _issue_rows(raw, invalid_year, "invalid_year", "year is not an integer")
    issues += _issue_rows(raw, invalid_month, "invalid_month", "month is not an integer")
    stats.invalid_year = int(invalid_year.sum())
    stats.invalid_month = int(invalid_month.sum())
    raw = raw.loc[~(invalid_year | invalid_month)].copy()
    raw["_year"] = raw["_year"].astype("int64")
    raw["_month"] = raw["_month"].astype("int64")

    year_range = raw["_year"].lt(MIN_YEAR) | raw["_year"].gt(MAX_YEAR)
    issues += _issue_rows(
        raw,
        year_range,
        "year_out_of_range",
        f"expected {MIN_YEAR}..{MAX_YEAR}",
    )
    stats.invalid_year += int(year_range.sum())
    raw = raw.loc[~year_range].copy()

    month_range = raw["_month"].lt(1) | raw["_month"].gt(12)
    issues += _issue_rows(raw, month_range, "month_out_of_range", "expected 1..12")
    stats.invalid_month += int(month_range.sum())
    raw = raw.loc[~month_range].copy()

    raw["_date"] = pd.to_datetime(
        {"year": raw["_year"], "month": raw["_month"], "day": 1}, errors="coerce"
    )
    bad_date = raw["_date"].isna()
    issues += _issue_rows(raw, bad_date, "invalid_date", "could not build a date")
    stats.invalid_year += int(bad_date.sum())
    raw = raw.loc[~bad_date].copy()

    raw["_value_text"] = raw["value"].astype("string").str.strip()
    missing_value = raw["_value_text"].isna() | (raw["_value_text"] == "")
    issues += _issue_rows(
        raw, missing_value, "missing_value", "blank is not treated as zero"
    )
    stats.missing_value = int(missing_value.sum())
    raw = raw.loc[~missing_value].copy()

    numeric_text = raw["_value_text"].str.replace(",", "", regex=False)
    raw["_numeric"] = pd.to_numeric(numeric_text, errors="coerce")
    invalid_numeric = raw["_numeric"].isna()
    issues += _issue_rows(
        raw, invalid_numeric, "invalid_numeric", "value is not a finite number"
    )
    stats.invalid_numeric = int(invalid_numeric.sum())
    raw = raw.loc[~invalid_numeric].copy()

    non_finite = raw["_numeric"].map(
        lambda value: not math.isfinite(float(value))
    )
    issues += _issue_rows(raw, non_finite, "non_finite_value")
    stats.invalid_numeric += int(non_finite.sum())
    raw = raw.loc[~non_finite].copy()

    negative = raw["_numeric"] < 0
    issues += _issue_rows(raw, negative, "negative_value")
    stats.negative_value = int(negative.sum())
    raw = raw.loc[~negative].copy()

    too_large = raw["_numeric"] > MAX_VALUE
    issues += _issue_rows(
        raw, too_large, "value_out_of_range", f"value exceeds {MAX_VALUE}"
    )
    stats.out_of_range_value = int(too_large.sum())
    raw = raw.loc[~too_large].copy()

    raw["_key"] = (
        raw["unit_code"].astype(str)
        + "|"
        + raw["_year"].astype(str)
        + "|"
        + raw["_month"].astype(str)
        + "|"
        + raw["statistic"].astype(str)
    )
    duplicate_mask = raw.duplicated(subset="_key", keep="first")
    key_counts = raw.groupby("_key", sort=False).size()
    stats.duplicate_keys = int((key_counts > 1).sum())

    if duplicate_mask.any():
        kept = (
            raw.loc[~duplicate_mask, ["_key", "_numeric"]]
            .drop_duplicates(subset="_key", keep="first")
            .set_index("_key")["_numeric"]
        )
        duplicate_values = raw["_key"].map(kept)
        exact_duplicate_mask = duplicate_mask & (raw["_numeric"] == duplicate_values)
        conflict_mask = duplicate_mask & ~exact_duplicate_mask
        stats.duplicate_exact_rows = int(exact_duplicate_mask.sum())
        stats.duplicate_conflict_rows = int(conflict_mask.sum())

        for idx, row in raw.loc[exact_duplicate_mask].iterrows():
            issues.append(
                {
                    "source_row": int(row["_source_row"]),
                    "issue": "exact_duplicate",
                    "detail": "duplicate of the first kept row for this key",
                    "unit_code": row["unit_code"],
                    "year": row["_year"],
                    "month": row["_month"],
                    "statistic": row["statistic"],
                    "value": row["_value_text"],
                }
            )
        for idx, row in raw.loc[conflict_mask].iterrows():
            kept_value = kept.get(row["_key"], "")
            issues.append(
                {
                    "source_row": int(row["_source_row"]),
                    "issue": "conflicting_duplicate",
                    "detail": f"kept first value {kept_value}; dropped {row['_numeric']}",
                    "unit_code": row["unit_code"],
                    "year": row["_year"],
                    "month": row["_month"],
                    "statistic": row["statistic"],
                    "value": row["_value_text"],
                }
            )
        raw = raw.loc[~duplicate_mask].copy()
        LOG.info(
            "Duplicates: %d keys, %d exact rows dropped, %d conflicting rows dropped",
            stats.duplicate_keys,
            stats.duplicate_exact_rows,
            stats.duplicate_conflict_rows,
        )

    unknown_mask = ~raw["statistic"].isin(STATISTIC_LABELS)
    stats.unknown_statistics = int(unknown_mask.sum())
    if stats.unknown_statistics:
        LOG.warning(
            "%d rows use statistic codes not in the documented label map",
            stats.unknown_statistics,
        )

    cleaned = pd.DataFrame(
        {
            "unit_code": raw["unit_code"].astype(str),
            "year": raw["_year"].astype("int64"),
            "month": raw["_month"].astype("int64"),
            "date": raw["_date"].dt.strftime("%Y-%m-%d"),
            "statistic": raw["statistic"].astype(str),
            "statistic_label": raw["statistic"].map(
                lambda code: STATISTIC_LABELS.get(code, "Unknown statistic")
            ),
            "value": raw["_numeric"],
        }
    )
    cleaned = cleaned.sort_values(
        ["unit_code", "date", "statistic"], kind="mergesort"
    ).reset_index(drop=True)

    if not cleaned.empty and (cleaned["value"] % 1 == 0).all():
        cleaned["value"] = cleaned["value"].astype("int64")
        stats.value_dtype = "integer"
    else:
        stats.value_dtype = "float"

    issues.sort(key=lambda item: int(item["source_row"]))
    stats.rows_written = len(cleaned)
    stats.issues_total = len(issues)
    stats.unit_count = int(cleaned["unit_code"].nunique()) if not cleaned.empty else 0
    if not cleaned.empty:
        stats.date_min = str(cleaned["date"].min())
        stats.date_max = str(cleaned["date"].max())
        stats.rows_by_statistic = {
            str(key): int(value)
            for key, value in cleaned["statistic"].value_counts().sort_index().items()
        }

    return cleaned, issues, stats


def _read_source_metadata(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not read source metadata %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def build_report(stats: QualityStats, *, generated_at: str) -> str:
    quality_rows = [
        ("Rows read", stats.rows_read),
        ("Rows written", stats.rows_written),
        ("Rows excluded by --statistics filter", stats.rows_filtered_by_statistic),
        ("Missing required columns", ", ".join(stats.missing_columns) or "none"),
        ("Rows dropped: missing unit code", stats.invalid_unit),
        ("Rows dropped: missing statistic", stats.invalid_statistic),
        ("Rows dropped: invalid/out-of-range year", stats.invalid_year),
        ("Rows dropped: invalid/out-of-range month", stats.invalid_month),
        ("Rows dropped: blank value (not coerced to 0)", stats.missing_value),
        ("Rows dropped: non-numeric/non-finite value", stats.invalid_numeric),
        ("Rows dropped: negative value", stats.negative_value),
        ("Rows dropped: value above sanity limit", stats.out_of_range_value),
        ("Duplicate keys", stats.duplicate_keys),
        ("Rows dropped: exact duplicates", stats.duplicate_exact_rows),
        ("Rows dropped: conflicting duplicates (first kept)", stats.duplicate_conflict_rows),
        ("Rows with unknown statistic code", stats.unknown_statistics),
    ]
    by_stat_rows = [
        (code, STATISTIC_LABELS.get(code, "Unknown statistic"), count)
        for code, count in stats.rows_by_statistic.items()
    ]

    source_meta = stats.source_metadata or {}
    package = source_meta.get("data_package", {}) if source_meta else {}
    source_page = source_meta.get("source_page", {}) if source_meta else {}
    provenance_rows = [
        ("Source file", stats.input_path),
        ("Input SHA-256", stats.input_sha256 or "not computed"),
        ("Cleaned output", stats.output_path),
        ("Output SHA-256", stats.output_sha256 or "not computed"),
        ("Official data package", package.get("title", "not supplied")),
        ("Package content range", f"{package.get('content_begin', '?')} to {package.get('content_end', '?')}"),
        ("Package published", package.get("published", "?")),
        ("Package DOI", package.get("doi", "?")),
        ("NPS page last updated", source_page.get("last_updated", "?")),
        ("Access date (UTC)", source_meta.get("access_date_utc", "?")),
    ]

    lines = [
        "# NPS Visitation Data Quality Report",
        "",
        f"Generated: {generated_at}",
        "",
        "## Summary",
        "",
        _markdown_table(["Metric", "Value"], [(a, b) for a, b in quality_rows]),
        "",
        f"- Distinct park/unit codes: **{stats.unit_count}**",
        f"- Date coverage: **{stats.date_min or 'n/a'}** through **{stats.date_max or 'n/a'}**",
        f"- Numeric value storage: **{stats.value_dtype}**",
        f"- Issue rows recorded: **{stats.issues_total}**"
        + (f" (details: `{stats.issues_path}`)" if stats.issues_path else ""),
        "",
        "## Provenance",
        "",
        _markdown_table(["Field", "Value"], provenance_rows),
        "",
        "## Rows by statistic",
        "",
        _markdown_table(["Code", "Meaning", "Rows"], by_stat_rows)
        if by_stat_rows
        else "No valid rows remained.",
        "",
        "## Cleaning rules",
        "",
        (
            "- Column names are lowercased, spaces/punctuation become underscores, "
            "and common aliases are mapped to the official fields."
        ),
        (
            "- `date` is derived as the first day of the official `Year`/`Month` "
            "period; it is not an observation day."
        ),
        (
            "- Blank values are treated as missing and dropped, never filled with "
            "zero. Zero is kept only when the source explicitly reports `0`."
        ),
        (
            "- Values are parsed after removing thousands separators; negative, "
            "non-finite, and absurdly large values are dropped and reported."
        ),
        (
            "- Duplicate keys are `(unit_code, year, month, statistic)`. The first "
            "valid row is kept. Exact duplicates are counted separately from "
            "conflicts where another row has a different value."
        ),
        ("- Rows are sorted by unit, date, and statistic in the cleaned output."),
        "",
        "## Known limitations",
        "",
        (
            "- NPS monthly records may be absent (no row) or blank (missing value); "
            "absence is not the same as zero visits."
        ),
        (
            "- Annual totals must be aggregated from monthly rows. Do not sum across "
            "statistics: `TRV`, `TRVH`, overnight stays, and total visits measure "
            "different things."
        ),
        (
            "- Reporting and counting procedures change over time; comparisons "
            "across decades should be interpreted cautiously."
        ),
        (
            "- The latest calendar year is finalized annual data; current-year data "
            "is preliminary and outside the package used here."
        ),
        ("- Park unit totals are not counts of unique people."),
        "",
    ]
    if stats.columns_dropped:
        lines.extend(
            [
                "Unexpected extra columns were ignored: "
                + ", ".join(stats.columns_dropped),
                "",
            ]
        )
    if stats.columns_normalized:
        rename_rows = [
            (original, normalized)
            for original, normalized in stats.columns_normalized.items()
        ]
        lines.extend(
            [
                "## Column normalization",
                "",
                _markdown_table(["Original", "Normalized"], rename_rows),
                "",
            ]
        )
    return "\n".join(lines)


def _write_csv_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    df.to_csv(temp, index=False, lineterminator="\n")
    temp.replace(path)


def _write_text_atomic(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)


def _parse_statistics(value: str) -> set[str] | None:
    if value.strip().lower() == "all":
        return None
    codes = {part.strip().upper() for part in value.split(",") if part.strip()}
    if not codes:
        raise DataQualityError("--statistics must be 'all' or comma-separated codes")
    unknown = sorted(codes - set(STATISTIC_LABELS))
    if unknown:
        raise DataQualityError(f"unknown statistic code(s): {', '.join(unknown)}")
    return codes


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize and quality-check the official NPS Main_Data.csv "
            "(UnitCode, Year, Month, Statistic, Value)."
        )
    )
    parser.add_argument("--input", type=Path, default=Path("data/raw/Main_Data.csv"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/nps_visitation_cleaned.csv"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/processed/nps_data_quality_report.md"),
    )
    parser.add_argument(
        "--issues",
        type=Path,
        default=Path("data/processed/nps_data_quality_issues.csv"),
    )
    parser.add_argument(
        "--statistics",
        default="all",
        help="'all' or comma-separated official codes, e.g. TRV,TRVH",
    )
    parser.add_argument(
        "--source-metadata",
        type=Path,
        default=None,
        help=(
            "optional source_metadata.json from the scraper; defaults to "
            "<input folder>/source_metadata.json when present"
        ),
    )
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        configure_logging(args.verbose, args.log_file)
    except OSError as exc:
        print(f"ERROR: cannot open log file: {exc}", file=sys.stderr)
        return 2

    try:
        statistics = _parse_statistics(args.statistics)
    except DataQualityError as exc:
        LOG.error("%s", exc)
        return 2

    resolved_paths = {
        args.input.resolve(),
        args.output.resolve(),
        args.report.resolve(),
        args.issues.resolve(),
    }
    if len(resolved_paths) != 4:
        LOG.error("--input, --output, --report, and --issues must be different paths")
        return 2

    if not args.input.exists():
        LOG.error("Input file not found: %s", args.input)
        LOG.error(
            "Run scraper/scrape_nps_visitation.py first in an environment with "
            "NPS network access, or pass --input explicitly."
        )
        return 2
    if not args.input.is_file():
        LOG.error("Input is not a file: %s", args.input)
        return 2

    try:
        raw = pd.read_csv(
            args.input,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
        )
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        LOG.error("Could not read CSV %s: %s", args.input, exc)
        return 2

    try:
        cleaned, issues, stats = clean_dataframe(raw, statistics=statistics)
    except DataQualityError as exc:
        LOG.error("Data quality/schema failure: %s", exc)
        return 2

    stats.input_path = str(args.input)
    stats.output_path = str(args.output)
    stats.issues_path = str(args.issues)
    stats.input_sha256 = sha256_file(args.input)
    metadata_path = args.source_metadata
    if metadata_path is None:
        candidate = args.input.parent / "source_metadata.json"
        metadata_path = candidate if candidate.exists() else None
    stats.source_metadata = _read_source_metadata(metadata_path)

    if cleaned.empty:
        LOG.error("No valid rows remained after cleaning; writing report and exiting 1")
        try:
            _write_csv_atomic(pd.DataFrame(columns=CLEANED_COLUMNS), args.output)
            stats.output_sha256 = sha256_file(args.output)
            issue_frame = pd.DataFrame(issues, columns=ISSUE_COLUMNS)
            _write_csv_atomic(issue_frame, args.issues)
            generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            report = build_report(stats, generated_at=generated_at)
            _write_text_atomic(report, args.report)
        except OSError as exc:
            LOG.error("Could not write output files: %s", exc)
            return 3
        return 1

    try:
        _write_csv_atomic(cleaned, args.output)
        stats.output_sha256 = sha256_file(args.output)
        issue_frame = pd.DataFrame(issues, columns=ISSUE_COLUMNS)
        _write_csv_atomic(issue_frame, args.issues)
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report = build_report(stats, generated_at=generated_at)
        _write_text_atomic(report, args.report)
    except OSError as exc:
        LOG.error("Could not write output files: %s", exc)
        return 3

    LOG.info(
        "Cleaned %d rows -> %s (%d units, %s to %s)",
        stats.rows_written,
        args.output,
        stats.unit_count,
        stats.date_min,
        stats.date_max,
    )
    LOG.info("Data-quality report: %s", args.report)
    LOG.info("Dropped/flagged rows: %s (%d rows)", args.issues, stats.issues_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
