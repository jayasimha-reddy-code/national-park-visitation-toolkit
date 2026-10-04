# Test fixtures

- `nps_visitor_use_data_page.html` is an abridged HTML fixture based on the
  official NPS Visitor Use Data page captured/checked on **2026-10-07**. It is
  not a byte-for-byte copy; it preserves the headings, facts, links, and Power
  BI embed needed for parser tests.
- `irma_data_package_2025.html` is an abridged HTML fixture based on the
  official IRMA DataStore reference profile **2317666** checked on
  **2026-10-07**. It contains file links and package metadata, not data values.
- `main_data_synthetic.csv` is **entirely synthetic**. Unit codes and values are
  invented for edge-case tests (duplicates, blanks, invalid months, negatives,
  thousands separators). It is **not NPS data** and must never be used for
  analysis or presented as official visitation counts.

A live data download must be run with `scraper/scrape_nps_visitation.py` in an
environment that can reach `irma.nps.gov` and `irmaservices.nps.gov`. The build
sandbox used for this repository could read the official pages through a
browser-like fetcher but could not establish TLS connections to NPS hosts from
Python/curl.
