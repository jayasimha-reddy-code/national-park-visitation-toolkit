# National Park Visitation Toolkit

I built this toolkit to pull, clean, and analyze official U.S. National Park Service (NPS) visitor use statistics. It includes a complete data pipeline and an analysis notebook that processes the raw dataset end to end.

1. `organizer/organize_files.py`: Safely sorts loose files into organized folders (runs in dry run mode by default).
2. `scraper/scrape_nps_visitation.py`: Inspects the official NPS data page, checks the IRMA data package, and downloads the primary CSV file.
3. `scraper/clean_visitation.py`: Cleans and normalizes the raw CSV, flags missing or duplicate records, and outputs a data quality report.
4. `notebooks/visitation_analysis.ipynb`: Generates six charts and summary findings from the cleaned data.

I kept this lightweight: no heavy databases, no cloud dependencies, and no fragile web scraping against dynamic dashboards. Just reproducible Python scripts and local analysis.

## Data Source and Provenance

| Item | Value |
| --- | --- |
| Official portal | [Visitor Use Data](https://www.nps.gov/subjects/socialscience/visitor-use-statistics-dashboard.htm), last updated 2026-03-26 |
| Official package | NPS Visitor Use Statistics Data Package, 2025 (IRMA reference 2317666, published 2026-03-26) |
| DOI / license | [10.57830/2317666](https://doi.org/10.57830/2317666) (CC0 1.0) |
| File processed | `Main_Data.csv` (DataStore file 756959) |
| File size and checksum | 72,212,002 bytes with SHA 256 `284ab33b15e9a817948cba4663ed33748ba24dbbc36b750be2b2cfbb662fe912` |
| Timeframe analyzed | 1979-01-01 to 2025-12-31 (monthly records per park unit) |
| Metric analyzed | `TRV` (recreation visits, measuring visits rather than unique individuals) |
| Cleaned record count | 2,905,377 rows kept out of 2,905,440 raw rows |
| Coverage | 406 park unit codes |

Calendar year 2026 numbers are still preliminary, so I excluded them from this analysis. Also, the 2024 Statistical Abstract on the NPS site is older than this 2025 data package, so 2024 is not the final year of available data.

### How I Retrieved the Data

When checking `irma.nps.gov/robots.txt`, I noted that crawling `/DataStore/DownloadFile/` is restricted. Furthermore, the public IRMA profile page (`/DataStore/Reference/Profile/2317666`) is an ExtJS JavaScript application that renders no file table in static HTML.

To retrieve the file cleanly and follow site rules, I wrote the scraper to read the landing page, detect the dynamic table structure, and query the public metadata using the documented DataStore v7 API:
`https://irmaservices.nps.gov/datastore/v7/rest/Profile/2317666`

From there, it makes a single targeted download for the public CC0 CSV:
`https://irmaservices.nps.gov/datastore/v7/rest/DownloadFile/756959?accessType=DOWNLOAD`

The script documents this entire handshake in `data/raw/source_metadata.json` with `"metadata_source": "datastore_api_v7"` and `"profile_page_is_javascript_driven": true`. I made sure not to scrape the embedded Power BI dashboard.

During local testing, my local environment threw TLS handshake errors (`SSLZeroReturnError`) when hitting NPS endpoints from Python. Rather than hacking local certificates, I ran the download, cleanup, and notebook execution on a GitHub Actions runner (`.github/workflows/refresh-data.yml`). Workflow run [39049862661](https://github.com/jayasimha-reddy-code/national-park-visitation-toolkit/actions/runs/39049862661) executed all steps successfully: test suite, download, cleaning, notebook execution, and output commits. I configured this workflow for manual dispatch only so it does not pull 72 MB from NPS servers unnecessarily. You can inspect the run output in `reports/refresh_scrape.log`.

Because `Main_Data.csv` (72 MB) and the cleaned CSV (186 MB) exceed standard GitHub file limits, I added them to `.gitignore`. The repository includes all metadata, data quality reports, exported charts, CSV tables, findings, and the executed notebook.

## Repository Structure

```text
organizer/organize_files.py          File sorter (defaults to dry run)
scraper/scrape_nps_visitation.py     Page validator and verified file downloader
scraper/clean_visitation.py          Data cleaning, validation, and quality reporting
notebooks/visitation_analysis.ipynb  Executed analysis notebook generating six charts
reports/figures/                     Saved charts in PNG format
reports/tables/                      Exported summary CSV tables
reports/findings.md                  Key findings and summary statistics
reports/analysis_summary.json        Structured summary of chart data
data/raw/                            Downloaded CSV (gitignored) and source metadata
data/processed/                      Cleaned CSV (gitignored), quality reports, and issues
data/cache/                          Polite local HTTP cache (gitignored)
tests/                               Pytest suite and test fixtures

```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install matplotlib nbconvert ipykernel

```

Tested on Python 3.10 and newer. Base requirements are `requests`, `beautifulsoup4`, `pandas`, and `pytest`. Rendering charts and executing the notebook also require `matplotlib`, `nbconvert`, and `ipykernel`.

## Pipeline Execution

```bash
# 1. Verify the official page and package, then download Main_Data.csv
python -m scraper.scrape_nps_visitation --outdir data/raw --cache-dir data/cache

# 2. Clean and validate the data
python -m scraper.clean_visitation \
  --input data/raw/Main_Data.csv \
  --output data/processed/nps_visitation_cleaned.csv \
  --report data/processed/nps_data_quality_report.md \
  --issues data/processed/nps_data_quality_issues.csv \
  --source-metadata data/raw/source_metadata.json

# 3. Execute the analysis notebook and export figures
jupyter nbconvert --to notebook --execute --inplace notebooks/visitation_analysis.ipynb

# 4. Run the test suite
python -m pytest -q

```

Passing `--dry-run` to the scraper prints verified URLs without writing files. The notebook requires the real cleaned dataset to execute and will raise an error if the file is missing rather than falling back to mock fixtures.

## The Six Visualizations

I built all six charts using only standard fields present in the official file (`UnitCode`, `Year`, `Month`, `Statistic`, `Value`).

| # | Output File | Description | Methodology |
| --- | --- | --- | --- |
| 1 | `reports/figures/01_annual_recreation_visits.png` | System wide recreation visits from 1979 to 2025 | Sum of monthly `TRV` rows per calendar year |
| 2 | `reports/figures/02_top10_units_2025.png` | Top 10 most visited park units in 2025 | Total `TRV` by `UnitCode`, ranked descending |
| 3 | `reports/figures/03_monthly_seasonality.png` | Average monthly visits across 2015 to 2025 | Average of total visits per calendar month |
| 4 | `reports/figures/04_2019_2020_2025_comparison.png` | Total visit volume for 2019, 2020, and 2025 | Annual totals and percentage shifts relative to 2019 |
| 5 | `reports/figures/05_park_change_2019_2025.png` | Distribution of park level changes from 2019 to 2025 | Filtered to comparable units only |
| 6 | `reports/figures/06_top10_share_over_time.png` | Concentration of total visits across top 10 parks | Top 10 roster recalculated annually |

For Chart 5, I implemented strict comparability rules directly in the plotting logic:

* Units must have valid `TRV` data in both 2019 and 2025.
* Growth calculation: `(2025 visits - 2019 visits) / 2019 visits * 100`.
* Zero baseline exclusion: Parks with zero visits in 2019 are excluded from percentage math to avoid division by zero and documented in the subtitle.
* Parks missing either comparison year are excluded and noted.
* The histogram displays changes between -100% and +250%, with extreme outliers summarized in the subtitle text.

Current dataset counts: **378 comparable units**, median change of **-10.0%**, interquartile range of **-27.8% to +10.3%**, **140 units increased**, **238 units decreased**, **27 units missing a comparison year**, **1 unit with a zero 2019 baseline**, and **3 units above +250%**.

Each figure includes clean axis labeling, units, source citations (dataset DOI and retrieval date), and legends.

## Key Analysis Findings

Detailed analysis is in `reports/findings.md`, and raw summary values are available in `reports/analysis_summary.json`.

1. **System Growth:** Annual recreation visits totaled 205.4 million in 1979 and reached 323.0 million in 2025. The historical peak was 331.9 million in 2024, putting 2025 roughly 2.7% below that high. Reporting units expanded from 271 in 1979 to 400 in 2025, meaning long term volume increases reflect both increased park popularity and an expanding system inventory.
2. **Top Parks in 2025:** The 10 most visited units accounted for 93.9 million visits (29.1% of the 323.0 million total). Blue Ridge Parkway (BLRI) led with 16.53 million visits, followed by Golden Gate National Recreation Area (GOGA) at 15.75 million and Great Smoky Mountains (GRSM) at 11.53 million.
3. **Seasonality:** Looking at 2015 to 2025 averages, July is the peak month with 42.5 million visits, while January dips to 12.7 million. This represents a 3.3x swing across seasons.
4. **Pre vs Post Pandemic Changes:** Visitation moved from 327.5 million in 2019 down to 237.1 million in 2020 (-27.6%), recovering to 323.0 million in 2025 (-1.4% compared to 2019).
5. **Park Level Variance:** Out of 378 comparable parks, 140 recorded higher visits in 2025 than in 2019, while 238 saw declines. Even though system wide totals were nearly flat (-1.4%), the median park changed by -10.0%, showing that system totals are heavily buffered by high volume parks.
6. **Visitor Concentration:** Over the entire 1979 to 2025 window, the top 10 parks absorbed between 28.0% and 40.8% of all recreation visits (peaking at 40.8% in 1981, and reaching a low of 28.0% in 2017). In 2025, that share was 29.1%. Because I recompute the top 10 cohort each year, this tracks system concentration rather than a static group of parks.

## Organizer Script

```bash
# Preview planned file moves without altering files (default behavior)
python organizer/organize_files.py --source ~/inbox --dest ~/organized

# Execute the moves
python organizer/organize_files.py --source ~/inbox --dest ~/organized --apply

```

* Categorizes loose files into subfolders: `documents/`, `images/`, `data/`, `archives/`, and `other/`. It operates strictly on top level files and will not alter directory trees.
* Defaults to a dry run. The script will only move files when passed the `--apply` flag. I tested this by running a dry run against a test folder with a PDF, JPG, CSV, hidden file, and symlink. All original files stayed untouched in place, the destination folder remained empty, and the output logged three planned moves.
* Skips hidden files and symlinks by default unless you pass `--include-hidden` or `--include-symlinks`.
* Safe naming: existing destination files are never overwritten. Conflicts are automatically renamed (`name_1.ext`, `name_2.ext`, etc.).
* Built in sanity checks: the script will refuse to run if the source and destination match, if paths nest inside each other, if pointing to root or home directories, or if targeting symlinked folders.
* Passing `--apply` writes an execution log to `<dest>/organize_files.log`, while dry runs print output to stderr.
* Standard exit codes: `0` for clean execution, `1` for move errors, and `2` for configuration or input validation failures.

## Scraper Implementation

* Built with `requests` and `BeautifulSoup` to parse official page metadata including page title, headers, description text, update dates, and IRMA links.
* Validates that the package `Content End Date` matches the current finalized year shown on the NPS portal. If the portal publishes a newer dataset, the scraper exits cleanly with instructions instead of pulling out of date references.
* Specifically targets `Main_Data.csv` and ignores auxiliary files like state level extracts or EML metadata.
* Generates `source_metadata.json` logging the exact API endpoint used, file checksums, download sizes, licenses, timestamps, and robots parameters.
* Checks cached downloads against source metadata and SHA 256 hashes to prevent redundant downloads. Use `--refresh` to force a new pull.

Network settings include a descriptive user agent, strict timeouts (10s connect, 120s read), exponential backoff across 3 retries, adherence to `Retry-After` headers, polite request throttling (minimum 1.5s delay plus robots directives), and local 24 hour caching with ETag validation. The scraper streams writes to `Main_Data.csv.part`, verifies CSV headers, and performs an atomic rename once finished to prevent partial downloads.

## Cleaning and Validation

* Cleans column headers (normalizes casing, spacing, punctuation, and aliases) and records dropped fields.
* Converts reporting dates into a standard `date` column matching the first day of each month. Strips number formatting and parses integers cleanly. Blank fields are left as missing values rather than assumed to be zero. Negative numbers, non finite values, and malformed entries are filtered out and written to an issues log.
* Deduplicates composite primary keys (`unit_code`, `year`, `month`, `statistic`). The script keeps the first occurrence, differentiates exact duplicate rows from key conflicts, and exports dropped rows for review.
* Appends human readable statistic labels, sorts data systematically, and produces a Markdown data quality report summarizing row counts, coverage, and edge cases.
* Standard exit codes: `1` if all rows fail validation, and `2` for input or schema errors.

When running on the official dataset, the script processed 2,905,440 source rows, outputting 2,905,377 clean records. It dropped 63 negative values (internal corrections across ten parks, primarily non recreation hours) and identified zero duplicate keys or missing required fields.

## Test Suite

```bash
python -m pytest -q

```

The test suite runs 55 unit and integration tests:

* Mock fixtures: `tests/fixtures/nps_visitor_use_data_page.html` and `irma_data_package_2025.html` reflect snapshots of the official sites checked on 2026-10-07. API metadata is mocked via `irma_profile_2317666.json`, and `main_data_synthetic.csv` provides clean synthetic test inputs.
* Tested features: HTML and API response parsing, year mismatch guards, single file filtering, cache handling, retry policies, robots checking, invalid payload detection, header checks, malformed data parsing, duplicate resolution, and file organizer edge cases.
* Tests run completely offline and will not perform live network calls or modify project files.

## Data Caveats

1. **Methodology Shifts Over Time:** NPS counting methodologies have evolved since 1979. Counting equipment, vehicle multiplier assumptions, and estimation techniques vary by decade and park type. Long term comparisons reflect both actual attendance patterns and adjustments in counting rigor.
2. **System Inventory Changes:** The dataset contains 406 distinct unit codes, while the NPS portal notes that 406 out of 433 existing units reported in 2025. The number of reporting units in the recreation series grew from 271 in 1979 to 400 in 2025. As parks are added to the national system, overall totals rise naturally.
3. **Missing Data vs Zero:** Empty cells indicate that a park did not submit a report for that period; they are left missing rather than filled with zeros. A missing month lowers that park's annual sum. The 63 negative correction values in `Main_Data.csv` were excluded during cleaning.
4. **Visits vs Unique Visitors:** Recreation visits measure gate and site entries rather than distinct visitors. A single person visiting multiple points of interest or returning across multiple days generates multiple visit counts.
5. **Timeline:** This analysis strictly covers January 1979 through December 2025. Preliminary 2026 numbers are not included.

## Citation

National Park Service, Social Science Program. Visitor Use Data. U.S. Department of the Interior. [https://www.nps.gov/subjects/socialscience/visitor-use-statistics-dashboard.htm](https://www.nps.gov/subjects/socialscience/visitor-use-statistics-dashboard.htm) (accessed 2026-10-07; page updated 2026-03-26).

Ziesler, P. 2026. NPS Visitor Use Statistics Data Package, 2025. National Park Service, Fort Collins, CO. [https://doi.org/10.57830/2317666](https://doi.org/10.57830/2317666). Data file `Main_Data.csv` (DataStore file 756959), CC0 1.0.

## Verification and Testing Log

Commands verified across local development and GitHub Actions runs:

| Command | Environment | Status / Result |
| --- | --- | --- |
| `pytest -q` | Local venv | 55 passed |
| `ruff check --target-version py310 organizer scraper tests` | Local venv | Clean |
| `python organizer/organize_files.py --source /tmp/org-check/src --dest /tmp/org-check/dest` | Local | Exit 0, dry run successful, 0 files moved, hidden and symlink items skipped |
| `python -m scraper.scrape_nps_visitation --outdir data/raw --cache-dir data/cache` | GitHub Actions | Page, package, and API verified; 72,212,002 bytes downloaded and verified against SHA 256 |
| `python -m scraper.clean_visitation --input data/raw/Main_Data.csv …` | GitHub Actions | 2,905,377 clean rows written, 63 issues logged |
| `jupyter nbconvert --to notebook --execute --inplace notebooks/visitation_analysis.ipynb` | GitHub Actions | Ran end to end; exported 6 PNG figures and 6 CSV tables |
| `python -m pytest -q` | GitHub Actions | All tests passed before download stage (documented in `reports/refresh_notebook.log`) |

Generated outputs: `data/raw/source_metadata.json`, `data/processed/nps_data_quality_report.md`, `data/processed/nps_data_quality_issues.csv`, six figures in `reports/figures/`, six data tables in `reports/tables/`, `reports/findings.md`, `reports/analysis_summary.json`, and the executed notebook. Raw and processed CSVs are generated locally through the pipeline scripts.
