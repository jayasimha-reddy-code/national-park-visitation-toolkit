# Findings: NPS Recreation Visits (1979 to 2025)

Built from the official **NPS Visitor Use Statistics Data Package, 2025** (IRMA 2317666,
DOI 10.57830/2317666, CC0), downloaded by `scraper/scrape_nps_visitation.py` and cleaned by
`scraper/clean_visitation.py`. Data period used: **January 1979 to December 2025**.
Figures: `reports/figures/`; tables: `reports/tables/`.

1. **Annual total.** Recreation visits were 205.4M in 1979 and
   323.0M in 2025; the series peak is 331.9M in
   2024. 2025 is -2.7% below the peak.
2. **Top units, 2025.** The ten most visited units recorded 93.9M visits
   (29.1% of the 323.0M total). BLRI led with
   16.5M.
3. **Seasonality.** Across 2015 to 2025, Jul averaged 42.5M visits
   and Jan averaged 12.7M.
4. **2019 vs 2020 vs 2025.** 327.5M to 237.1M
   (-27.6%) to 323.0M (-1.4% vs 2019).
5. **Park level change, 2019 to 2025.** 378 comparable units; median
   -10.0% (IQR -27.8% to +10.3%);
   140 up, 238 down. Excluded: 27 units missing a year, 1 with a
   zero 2019 baseline.
6. **Concentration.** The annual top ten share was 28.0% to 40.8%
   over 1979 to 2025 and 29.1% in 2025.

These are descriptive results. The data do not identify causes of changes in visitation.

## Limitations

Counting procedures and the roster of reporting units changed over 1979 to 2025; missing
records are not zero-filled; percent change is undefined for zero or missing 2019
baselines and such units are not ranked; visits are entries, not unique people. See the
notebook limitations section and `data/processed/nps_data_quality_report.md`.
