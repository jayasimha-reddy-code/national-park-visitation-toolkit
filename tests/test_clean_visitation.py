from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scraper.clean_visitation import (
    CLEANED_COLUMNS,
    ISSUE_COLUMNS,
    DataQualityError,
    clean_dataframe,
    main,
    normalize_column_name,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC = FIXTURES / "main_data_synthetic.csv"


def read_fixture(path: Path = SYNTHETIC) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)


def test_normalize_column_name() -> None:
    assert normalize_column_name(" Unit Code ") == "unit_code"
    assert normalize_column_name("YEAR") == "year"
    assert normalize_column_name("Park Code") == "unit_code"
    assert normalize_column_name("Value") == "value"


def test_cleaning_counts_and_rules() -> None:
    cleaned, issues, stats = clean_dataframe(read_fixture())

    assert stats.rows_read == 18
    assert stats.rows_written == 9
    assert stats.rows_filtered_by_statistic == 0
    assert stats.duplicate_keys == 2
    assert stats.duplicate_exact_rows == 1
    assert stats.duplicate_conflict_rows == 1
    assert stats.missing_value == 1
    assert stats.invalid_numeric == 1
    assert stats.invalid_month == 2
    assert stats.negative_value == 1
    assert stats.invalid_year == 1
    assert stats.out_of_range_value == 1
    assert stats.unknown_statistics == 1
    assert stats.unit_count == 5
    assert stats.date_min == "2023-06-01"
    assert stats.date_max == "2025-12-01"
    assert stats.rows_by_statistic == {"TRV": 7, "TRVH": 1, "XYZ": 1}
    assert len(issues) == 9
    assert list(cleaned.columns) == CLEANED_COLUMNS

    # Exact duplicate dropped, conflicting duplicate resolved to the first value.
    aaaa_march = cleaned[
        (cleaned["unit_code"] == "AAAA")
        & (cleaned["date"] == "2024-03-01")
        & (cleaned["statistic"] == "TRV")
    ]
    assert len(aaaa_march) == 1
    assert aaaa_march.iloc[0]["value"] == 1200
    aaaa_april = cleaned[
        (cleaned["unit_code"] == "AAAA") & (cleaned["date"] == "2024-04-01")
    ]
    assert aaaa_april.iloc[0]["value"] == 1300

    # Thousands separators are parsed; zero is preserved; blank is not zero.
    bbbb = cleaned[(cleaned["unit_code"] == "BBBB") & (cleaned["date"] == "2023-06-01")]
    assert bbbb.iloc[0]["value"] == 1234
    ffff = cleaned[cleaned["unit_code"] == "FFFF"]
    assert ffff.iloc[0]["value"] == 0
    assert len(cleaned[cleaned["unit_code"] == "BBBB"]) == 1
    assert cleaned[cleaned["unit_code"] == "BBBB"].iloc[0]["value"] == 1234

    # Labels and date derivation.
    trv = cleaned[cleaned["statistic"] == "TRV"].iloc[0]
    assert trv["statistic_label"] == "Recreation Visits"
    assert trv["date"].endswith("-01")
    unknown = cleaned[cleaned["statistic"] == "XYZ"]
    assert unknown.iloc[0]["statistic_label"] == "Unknown statistic"


def test_cleaning_with_statistic_filter() -> None:
    cleaned, issues, stats = clean_dataframe(read_fixture(), statistics={"TRV"})
    assert stats.rows_read == 18
    assert stats.rows_filtered_by_statistic == 2
    assert stats.rows_written == 7
    assert set(cleaned["statistic"]) == {"TRV"}
    # TRVH and XYZ were intentionally excluded, not reported as quality failures.
    assert stats.unknown_statistics == 0
    assert all(issue["issue"] != "unknown_statistic" for issue in issues)


def test_missing_required_column_raises() -> None:
    raw = pd.DataFrame(
        {
            "UnitCode": ["AAAA"],
            "Year": ["2024"],
            "Statistic": ["TRV"],
            "Value": ["1"],
        }
    )
    with pytest.raises(DataQualityError, match="month"):
        clean_dataframe(raw)


def test_column_aliases_and_extra_columns() -> None:
    raw = pd.DataFrame(
        {
            " Unit Code ": ["aaaa"],
            "YEAR": ["2024"],
            "Month": ["1"],
            "Statistic": ["trv"],
            "Value": ["10"],
            "Notes": ["ignored"],
        }
    )
    cleaned, issues, stats = clean_dataframe(raw)
    assert stats.columns_normalized[" Unit Code "] == "unit_code"
    assert stats.columns_dropped == ["notes"]
    assert len(cleaned) == 1
    assert cleaned.iloc[0]["unit_code"] == "AAAA"
    assert cleaned.iloc[0]["value"] == 10
    assert issues == []


def test_duplicate_classification_uses_first_kept_value() -> None:
    raw = pd.DataFrame(
        {
            "UnitCode": ["AAAA", "AAAA", "AAAA"],
            "Year": ["2024", "2024", "2024"],
            "Month": ["1", "1", "1"],
            "Statistic": ["TRV", "TRV", "TRV"],
            "Value": ["10", "10", "99"],
        }
    )
    cleaned, issues, stats = clean_dataframe(raw)
    assert len(cleaned) == 1
    assert cleaned.iloc[0]["value"] == 10
    assert stats.duplicate_exact_rows == 1
    assert stats.duplicate_conflict_rows == 1
    assert {issue["issue"] for issue in issues} == {
        "conflicting_duplicate",
        "exact_duplicate",
    }


def test_cli_writes_cleaned_csv_report_and_issues(tmp_path: Path) -> None:
    output = tmp_path / "cleaned.csv"
    report = tmp_path / "report.md"
    issues = tmp_path / "issues.csv"
    rc = main(
        [
            "--input",
            str(SYNTHETIC),
            "--output",
            str(output),
            "--report",
            str(report),
            "--issues",
            str(issues),
        ]
    )
    assert rc == 0
    result = pd.read_csv(output)
    assert len(result) == 9
    assert list(result.columns) == CLEANED_COLUMNS
    issue_frame = pd.read_csv(issues)
    assert list(issue_frame.columns) == ISSUE_COLUMNS
    assert len(issue_frame) == 9
    text = report.read_text(encoding="utf-8")
    assert "NPS Visitation Data Quality Report" in text
    assert "| Rows read | 18 |" in text
    assert "| Rows written | 9 |" in text
    assert "Distinct park/unit codes: **5**" in text
    assert "2023-06-01" in text
    assert "2025-12-01" in text


def test_cli_statistics_filter(tmp_path: Path) -> None:
    output = tmp_path / "cleaned.csv"
    report = tmp_path / "report.md"
    issues = tmp_path / "issues.csv"
    rc = main(
        [
            "--input",
            str(SYNTHETIC),
            "--output",
            str(output),
            "--report",
            str(report),
            "--issues",
            str(issues),
            "--statistics",
            "TRV",
        ]
    )
    assert rc == 0
    result = pd.read_csv(output)
    assert len(result) == 7
    assert set(result["statistic"]) == {"TRV"}
    assert "Rows excluded by --statistics filter | 2" in report.read_text()


def test_all_invalid_rows_exit_one_but_write_report(tmp_path: Path) -> None:
    source = tmp_path / "bad.csv"
    source.write_text("UnitCode,Year,Month,Statistic,Value\nAAAA,2024,1,TRV,\n")
    output = tmp_path / "cleaned.csv"
    report = tmp_path / "report.md"
    issues = tmp_path / "issues.csv"
    rc = main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--report",
            str(report),
            "--issues",
            str(issues),
        ]
    )
    assert rc == 1
    assert output.read_text().strip() == ",".join(CLEANED_COLUMNS)
    assert "No valid rows remained" in report.read_text()
    assert len(pd.read_csv(issues)) == 1


def test_bad_statistic_code_and_missing_input(tmp_path: Path) -> None:
    common = [
        "--output",
        str(tmp_path / "out.csv"),
        "--report",
        str(tmp_path / "report.md"),
        "--issues",
        str(tmp_path / "issues.csv"),
    ]
    assert main(["--input", str(SYNTHETIC), *common, "--statistics", "NOPE"]) == 2
    assert main(["--input", str(tmp_path / "missing.csv"), *common]) == 2


def test_report_includes_source_metadata(tmp_path: Path) -> None:
    metadata = {
        "data_package": {
            "title": "NPS Visitor Use Statistics Data Package, 2025",
            "content_begin": "1979-01-01",
            "content_end": "2025-12-31",
            "published": "2026-03-26",
            "doi": "10.57830/2317666",
        },
        "source_page": {"last_updated": "2026-03-26"},
        "access_date_utc": "2026-10-07",
    }
    meta_path = tmp_path / "source_metadata.json"
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    output = tmp_path / "cleaned.csv"
    report = tmp_path / "report.md"
    rc = main(
        [
            "--input",
            str(SYNTHETIC),
            "--output",
            str(output),
            "--report",
            str(report),
            "--issues",
            str(tmp_path / "issues.csv"),
            "--source-metadata",
            str(meta_path),
        ]
    )
    assert rc == 0
    text = report.read_text()
    assert "10.57830/2317666" in text
    assert "2026-10-07" in text
    assert "1979-01-01 to 2025-12-31" in text


def test_refuses_output_paths_that_collide(tmp_path: Path) -> None:
    same = tmp_path / "same.csv"
    rc = main(
        [
            "--input",
            str(SYNTHETIC),
            "--output",
            str(same),
            "--report",
            str(same),
            "--issues",
            str(tmp_path / "issues.csv"),
        ]
    )
    assert rc == 2


def test_empty_csv_file_is_a_clear_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    rc = main(
        [
            "--input",
            str(empty),
            "--output",
            str(tmp_path / "out.csv"),
            "--report",
            str(tmp_path / "report.md"),
            "--issues",
            str(tmp_path / "issues.csv"),
        ]
    )
    assert rc == 2
    assert not (tmp_path / "out.csv").exists()


def test_header_only_file_writes_outputs_and_exits_one(tmp_path: Path) -> None:
    header_only = tmp_path / "header_only.csv"
    header_only.write_text("UnitCode,Year,Month,Statistic,Value\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    report = tmp_path / "report.md"
    issues = tmp_path / "issues.csv"
    rc = main(
        [
            "--input",
            str(header_only),
            "--output",
            str(out),
            "--report",
            str(report),
            "--issues",
            str(issues),
        ]
    )
    assert rc == 1
    assert out.read_text(encoding="utf-8") == ",".join(CLEANED_COLUMNS) + "\n"
    assert report.exists() and "Rows read | 0" in report.read_text(encoding="utf-8")
    assert issues.exists()


def test_malformed_dates_and_non_numeric_values_are_rejected_not_guessed() -> None:
    raw = pd.DataFrame(
        {
            "UnitCode": ["AAAA", "AAAA", "AAAA"],
            "Year": ["2020", "2020", "2020"],
            "Month": ["13", "02", "00"],
            "Statistic": ["TRV", "TRV", "TRV"],
            "Value": ["100", "not-a-number", "50"],
        }
    )
    cleaned, issues, stats = clean_dataframe(raw)
    assert cleaned.empty
    assert stats.invalid_month == 2
    assert stats.invalid_numeric == 1
    reasons = {issue["issue"] for issue in issues}
    assert "month_out_of_range" in reasons
    assert "invalid_numeric" in reasons
