# NPS Visitation Data Quality Report

Generated: 2026-10-07T16:33:50Z

## Summary

| Metric | Value |
| --- | --- |
| Rows read | 2905440 |
| Rows written | 2905377 |
| Rows excluded by --statistics filter | 0 |
| Missing required columns | none |
| Rows dropped: missing unit code | 0 |
| Rows dropped: missing statistic | 0 |
| Rows dropped: invalid/out-of-range year | 0 |
| Rows dropped: invalid/out-of-range month | 0 |
| Rows dropped: blank value (not coerced to 0) | 0 |
| Rows dropped: non-numeric/non-finite value | 0 |
| Rows dropped: negative value | 63 |
| Rows dropped: value above sanity limit | 0 |
| Duplicate keys | 0 |
| Rows dropped: exact duplicates | 0 |
| Rows dropped: conflicting duplicates (first kept) | 0 |
| Rows with unknown statistic code | 0 |

- Distinct park/unit codes: **406**
- Date coverage: **1979-01-01** through **2025-12-01**
- Numeric value storage: **integer**
- Issue rows recorded: **63** (details: `data/processed/nps_data_quality_issues.csv`)

## Provenance

| Field | Value |
| --- | --- |
| Source file | data/raw/Main_Data.csv |
| Input SHA-256 | 284ab33b15e9a817948cba4663ed33748ba24dbbc36b750be2b2cfbb662fe912 |
| Cleaned output | data/processed/nps_visitation_cleaned.csv |
| Output SHA-256 | f940b26a986e4353738f7c8a2e2f0e919b9169742952b4520ab01174be327279 |
| Official data package | NPS Visitor Use Statistics Data Package, 2025 |
| Package content range | 1979-01-01 to 2025-12-31 |
| Package published | 2026-03-26 |
| Package DOI | 10.57830/2317666 |
| NPS page last updated | 2026-03-26 |
| Access date (UTC) | 2026-10-07 |

## Rows by statistic

| Code | Meaning | Rows |
| --- | --- | --- |
| BC | Backcountry Overnight Stays | 193696 |
| CCG | Concessionaire Operated Campground Overnight Stays | 193696 |
| CL | Concessionaire Operated Lodging Overnight Stays | 193696 |
| MISC | Miscellaneous Overnight Stays | 193696 |
| NROS | Non-Recreation Overnight Stays | 193696 |
| TH | Visitor Hours (Recreation plus Non-Recreation) | 193688 |
| TNRV | Non-Recreation Visits | 193676 |
| TNRVH | Non-Recreation Visitor Hours | 193676 |
| TOS | Total Overnight Stays | 193696 |
| TRV | Recreation Visits | 193692 |
| TRVH | Recreation Visitor Hours | 193688 |
| TRVS | NPS Operated Campground RV Overnight Stays | 193696 |
| TT | NPS Operated Campground Tent Overnight Stays | 193696 |
| TTRV | NPS Operated Campground Overnight Stays (Tent plus RV) | 193696 |
| TV | Total Visits (Recreation plus Non-Recreation) | 193693 |

## Cleaning rules

- Column names are lowercased, spaces/punctuation become underscores, and common aliases are mapped to the official fields.
- `date` is derived as the first day of the official `Year`/`Month` period; it is not an observation day.
- Blank values are treated as missing and dropped, never filled with zero. Zero is kept only when the source explicitly reports `0`.
- Values are parsed after removing thousands separators; negative, non-finite, and absurdly large values are dropped and reported.
- Duplicate keys are `(unit_code, year, month, statistic)`. The first valid row is kept. Exact duplicates are counted separately from conflicts where another row has a different value.
- Rows are sorted by unit, date, and statistic in the cleaned output.

## Known limitations

- NPS monthly records may be absent (no row) or blank (missing value); absence is not the same as zero visits.
- Annual totals must be aggregated from monthly rows. Do not sum across statistics: `TRV`, `TRVH`, overnight stays, and total visits measure different things.
- Reporting and counting procedures change over time; comparisons across decades should be interpreted cautiously.
- The latest calendar year is finalized annual data; current-year data is preliminary and outside the package used here.
- Park unit totals are not counts of unique people.

## Column normalization

| Original | Normalized |
| --- | --- |
| UnitCode | unit_code |
| Year | year |
| Month | month |
| Statistic | statistic |
| Value | value |
