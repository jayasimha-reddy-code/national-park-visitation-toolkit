#!/usr/bin/env python3
"""Verify and download the official NPS Visitor Use Statistics data package.

The NPS "Visitor Use Data" page is inspected as HTML with Requests +
BeautifulSoup.  It links to the official IRMA data portal.  The IRMA Power BI
dashboard is JavaScript-driven, so its charts/tables are deliberately NOT
scraped.  Instead this tool verifies the official IRMA DataStore data-package
profile and downloads exactly one file, ``Main_Data.csv`` (monthly unit-level
statistics, 1979 through the latest finalized calendar year).

Why the DataStore REST API for the file?
  irma.nps.gov/robots.txt disallows automated crawling of
  /DataStore/DownloadFile/.  This tool does not crawl that path.  It makes one
  explicit, user-requested retrieval of a public CC0 data file through the
  documented DataStore REST API, which is intended for programmatic clients.
  HTML discovery still happens on the official public web page.  Use
  ``--dry-run`` to inspect and print the official URL without downloading.

No login, cookies, access-control bypass, parallel requests, or dashboard
scraping is performed.  Requests are serial, rate-limited, retried with
bounded backoff, cached, and given explicit timeouts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

TOOL = "scraper/scrape_nps_visitation.py"
DEFAULT_PAGE_URL = (
    "https://www.nps.gov/subjects/socialscience/"
    "visitor-use-statistics-dashboard.htm"
)
# Verified 2026-10-07: "NPS Visitor Use Statistics Data Package, 2025"
# (Reference 2317666), published 2026-03-26, content 1979-01-01..2025-12-31.
DEFAULT_PACKAGE_URL = "https://irma.nps.gov/DataStore/Reference/Profile/2317666"
DEFAULT_API_BASE = "https://irmaservices.nps.gov/datastore/v7"
DEFAULT_USER_AGENT = (
    "nps-visitation-toolkit/0.1 "
    "(+https://github.com/jayasimha-reddy-code/national-park-visitation-toolkit; "
    "research use)"
)
EXPECTED_CSV_COLUMNS = ["UnitCode", "Year", "Month", "Statistic", "Value"]
EXPECTED_CSV_HEADER = ",".join(EXPECTED_CSV_COLUMNS)
MAIN_FILE_NAME = "Main_Data.csv"
PAGE_HOSTS = {"www.nps.gov", "nps.gov"}
PACKAGE_HOSTS = {"irma.nps.gov"}
API_HOSTS = {"irmaservices.nps.gov"}
OFFICIAL_DOWNLOAD_SUFFIX = ".nps.gov"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
CACHE_VERSION = 1

LOG = logging.getLogger("nps_scraper")


class ScrapeError(RuntimeError):
    """The scrape could not be completed safely or verifiably."""


class HttpError(ScrapeError):
    """A request failed after bounded retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SafetyError(ScrapeError):
    """A URL or path failed an official-host/safety check."""


@dataclass
class Fetched:
    url: str
    content: bytes
    status_code: int
    headers: dict[str, str]
    fetched_at: str
    from_cache: bool = False


@dataclass
class Link:
    text: str
    url: str


@dataclass
class NpsPageInfo:
    source_url: str
    title: str
    h1: str
    description: str
    last_updated: str | None
    last_updated_text: str | None
    at_a_glance_year: int | None
    headline_recreation_visits_millions: float | None
    units_reporting: int | None
    units_total: int | None
    dashboard_platform: str | None
    dashboard_is_javascript_driven: bool
    static_dashboard_table_found: bool
    latest_abstract_year: int | None
    limitations_noted: list[str] = field(default_factory=list)
    portal_links: list[Link] = field(default_factory=list)
    data_download_links: list[Link] = field(default_factory=list)
    package_profile_links: list[Link] = field(default_factory=list)
    dashboard_links: list[Link] = field(default_factory=list)


@dataclass
class PackageFile:
    code: str | None
    file_name: str
    url: str
    size_text: str | None
    size_bytes_estimate: int | None
    row_text: str


@dataclass
class DataPackageInfo:
    source_url: str
    title: str
    reference_code: str | None
    package_year: int | None
    published: str | None
    content_begin: str | None
    content_end: str | None
    license: str | None
    doi: str | None
    files: list[PackageFile] = field(default_factory=list)
    metadata_source: str = "html"
    metadata_api_url: str | None = None
    profile_page_is_javascript_driven: bool = False


@dataclass
class DownloadInfo:
    path: Path
    bytes_written: int
    sha256: str
    url: str
    from_existing: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def require_https_host(url: str, allowed: set[str], label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SafetyError(f"{label} must use https: {url}")
    if parsed.hostname is None or parsed.hostname.lower() not in allowed:
        raise SafetyError(
            f"{label} must be on an official host {sorted(allowed)}: {url}"
        )


def is_official_nps_host(host: str) -> bool:
    return host == "nps.gov" or host.endswith(OFFICIAL_DOWNLOAD_SUFFIX)


def parse_human_date(text: str | None) -> str | None:
    if not text:
        return None
    value = clean_text(text)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _first_match(pattern: str, text: str, flags: int = re.IGNORECASE) -> re.Match[str] | None:
    return re.search(pattern, text, flags)


def parse_nps_page(html: bytes, source_url: str) -> NpsPageInfo:
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    h1_tag = soup.find("h1")
    h1 = clean_text(h1_tag.get_text(" ")) if h1_tag else ""
    description_tag = soup.find("meta", attrs={"name": "description"})
    description = (
        clean_text(description_tag.get("content", ""))
        if description_tag and description_tag.get("content")
        else ""
    )
    text = clean_text(soup.get_text(" "))

    last_updated_text = None
    updated_match = _first_match(r"Last updated:\s*([A-Za-z]+ \d{1,2}, \d{4})", text)
    if updated_match:
        last_updated_text = updated_match.group(1)
    last_updated = parse_human_date(last_updated_text)

    glance_match = _first_match(r"(\d{4})\s+Visitation At-A-Glance", text)
    at_a_glance_year = int(glance_match.group(1)) if glance_match else None
    if at_a_glance_year is None:
        fallback = _first_match(
            r"recreation visits in calendar year\s+(\d{4})", text
        )
        if fallback:
            at_a_glance_year = int(fallback.group(1))

    headline = None
    headline_match = _first_match(
        r"([0-9][0-9,.]*)\s+million recreation visits in calendar year", text
    )
    if headline_match:
        try:
            headline = float(headline_match.group(1).replace(",", ""))
        except ValueError:
            headline = None

    units_reporting = units_total = None
    units_match = _first_match(
        r"(\d+)\s+of the\s+(\d+)\s+National Park System units report visitor use",
        text,
    )
    if units_match:
        units_reporting = int(units_match.group(1))
        units_total = int(units_match.group(2))

    iframe_sources = [
        clean_text(tag.get("src", ""))
        for tag in soup.find_all("iframe")
        if tag.get("src")
    ]
    power_bi = any(
        "powerbi" in source.lower() or "powerbigov" in source.lower()
        for source in iframe_sources
    )
    dashboard_platform = "Microsoft Power BI" if power_bi or "Power BI" in text else None
    dashboard_is_javascript_driven = bool(dashboard_platform)

    static_dashboard_table = False
    for table in soup.find_all("table"):
        header_text = clean_text(table.get_text(" ")).lower()
        if "recreation visits" in header_text and "park" in header_text:
            rows = table.find_all("tr")
            if len(rows) > 2:
                static_dashboard_table = True
                break

    abstract_year = None
    abstract_match = _first_match(r"(\d{4})\s+Statistical Abstract", text)
    if abstract_match:
        abstract_year = int(abstract_match.group(1))

    limitations = [
        phrase
        for phrase in (
            "Current Year Data Warning",
            "Reporting Changes",
            "Count Procedure Changes",
            "Varying degrees of data quality",
            "Rounding Difference",
            "Potential Errors",
        )
        if phrase.lower() in text.lower()
    ]

    portal_links: list[Link] = []
    download_links: list[Link] = []
    profile_links: list[Link] = []
    dashboard_links: list[Link] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(source_url, anchor["href"].strip())
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        link = Link(clean_text(anchor.get_text(" ")), url)
        host = hostname(url)
        path = urlparse(url).path
        if host in PACKAGE_HOSTS and path.startswith("/Stats"):
            portal_links.append(link)
        elif host in PACKAGE_HOSTS and path.startswith("/DataStore/DownloadFile/"):
            download_links.append(link)
        elif host in PACKAGE_HOSTS and path.startswith("/DataStore/Reference/Profile/"):
            profile_links.append(link)
        if (
            "powerbi" in host
            or "visitation-statistics" in path.lower()
            or "visitor-use-statistics-dashboard" in path.lower()
        ):
            dashboard_links.append(link)

    for source in iframe_sources:
        if source.startswith(("http://", "https://")):
            dashboard_links.append(
                Link(text="embedded dashboard iframe", url=urljoin(source_url, source))
            )

    return NpsPageInfo(
        source_url=source_url,
        title=title,
        h1=h1,
        description=description,
        last_updated=last_updated,
        last_updated_text=last_updated_text,
        at_a_glance_year=at_a_glance_year,
        headline_recreation_visits_millions=headline,
        units_reporting=units_reporting,
        units_total=units_total,
        dashboard_platform=dashboard_platform,
        dashboard_is_javascript_driven=dashboard_is_javascript_driven,
        static_dashboard_table_found=static_dashboard_table,
        latest_abstract_year=abstract_year,
        limitations_noted=limitations,
        portal_links=portal_links,
        data_download_links=download_links,
        package_profile_links=profile_links,
        dashboard_links=dashboard_links,
    )


def _size_to_bytes(size_text: str | None) -> int | None:
    if not size_text:
        return None
    match = re.search(r"([\d.]+)\s*(B|KB|MB|GB)", size_text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[unit]
    return int(value * multiplier)


def parse_data_package(html: bytes, source_url: str) -> DataPackageInfo:
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" "))
    title_tag = soup.title.get_text(" ") if soup.title else ""
    title = clean_text(title_tag)
    title = re.sub(r"^NPS Datastore\s*-\s*", "", title, flags=re.IGNORECASE)
    title_match = _first_match(
        r"NPS Visitor Use Statistics Data Package,\s*(\d{4})", text
    )
    if title_match:
        package_year = int(title_match.group(1))
        if "Visitor Use Statistics Data Package" not in title:
            title = f"NPS Visitor Use Statistics Data Package, {package_year}"
    else:
        package_year = None

    code_match = _first_match(r"Data Package \(Code:\s*(\d+)\)", text)
    reference_code = code_match.group(1) if code_match else None
    if reference_code is None:
        profile_match = re.search(r"/Reference/Profile/(\d+)", source_url)
        reference_code = profile_match.group(1) if profile_match else None

    def labelled_date(label: str) -> str | None:
        match = re.search(
            rf"{re.escape(label)}\s+([A-Za-z]+ \d{{1,2}},\s+\d{{4}})", text
        )
        return parse_human_date(match.group(1)) if match else None

    published = labelled_date("Date Published")
    content_begin = labelled_date("Content Begin Date")
    content_end = labelled_date("Content End Date")

    license_name = None
    license_match = _first_match(r"Creative Commons Zero[^.;]*", text)
    if license_match:
        license_name = clean_text(license_match.group(0))
    elif "public domain" in text.lower():
        license_name = "Public domain"

    doi = None
    doi_match = re.search(r"10\.\d{4,9}/[^\s<>\"]+", text)
    if doi_match:
        doi = doi_match.group(0).rstrip(".,;)")

    files: list[PackageFile] = []
    for anchor in soup.find_all("a", href=True):
        url = urljoin(source_url, anchor["href"].strip())
        parsed = urlparse(url)
        if parsed.hostname not in PACKAGE_HOSTS:
            continue
        if not parsed.path.startswith("/DataStore/DownloadFile/"):
            continue
        file_name = clean_text(anchor.get_text(" "))
        if not re.search(r"\.[A-Za-z0-9]{2,5}$", file_name):
            continue
        code_match = re.search(r"/DownloadFile/(\d+)", parsed.path)
        row = anchor.find_parent("tr")
        row_text = clean_text(row.get_text(" ")) if row else ""
        size_match = re.search(
            r"([\d.]+\s*(?:KB|MB|GB|B))\b", row_text, re.IGNORECASE
        )
        size_text = size_match.group(1) if size_match else None
        files.append(
            PackageFile(
                code=code_match.group(1) if code_match else None,
                file_name=file_name,
                url=url,
                size_text=size_text,
                size_bytes_estimate=_size_to_bytes(size_text),
                row_text=row_text,
            )
        )

    return DataPackageInfo(
        source_url=source_url,
        title=title,
        reference_code=reference_code,
        package_year=package_year,
        published=published,
        content_begin=content_begin,
        content_end=content_end,
        license=license_name,
        doi=doi,
        files=files,
    )


def parse_package_json(payload: bytes, profile_url: str, api_url: str) -> DataPackageInfo:
    """Parse the official DataStore v7 ``/rest/Profile/{id}`` JSON response.

    The public profile HTML page at ``irma.nps.gov/DataStore/Reference/Profile``
    is an ExtJS shell that builds the page in the browser; the file table is not
    present in the static HTML.  The documented DataStore API serves the same
    public metadata as JSON, which is what this parser consumes.
    """
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScrapeError(f"package API did not return JSON ({api_url}): {exc}") from exc
    if not isinstance(document, dict):
        raise ScrapeError(f"package API returned an unexpected JSON document: {api_url}")

    bibliography = document.get("bibliography") or {}
    title = clean_text(str(bibliography.get("title") or ""))
    year_match = re.search(r"Data Package,\s*(\d{4})", title)
    package_year = int(year_match.group(1)) if year_match else None
    if not package_year:
        package_year = _year_from_iso((bibliography.get("contentEnd") or {}).get("yyyymmdd"))

    def iso_date(section: str) -> str | None:
        block = bibliography.get(section) or {}
        value = block.get("yyyymmdd") if isinstance(block, dict) else None
        return str(value) if value else None

    files: list[PackageFile] = []
    for entry in document.get("filesAndLinks") or []:
        if not isinstance(entry, dict):
            continue
        file_name = clean_text(str(entry.get("fileName") or ""))
        url = str(entry.get("url") or "")
        code = entry.get("fileId")
        if not file_name or not url:
            continue
        parsed = urlparse(url)
        if parsed.hostname not in PACKAGE_HOSTS:
            continue
        if not parsed.path.startswith("/DataStore/DownloadFile/"):
            continue
        files.append(
            PackageFile(
                code=str(code) if code is not None else None,
                file_name=file_name,
                url=url,
                size_text=None,
                size_bytes_estimate=None,
                row_text=clean_text(str(entry.get("description") or "")),
            )
        )

    citation = str(document.get("citation") or "")
    doi_match = re.search(r"10\.\d{4,9}/[^\s<>\"']+", citation)
    license_name = (bibliography.get("licenseType") or document.get("licenseType") or None)
    return DataPackageInfo(
        source_url=profile_url,
        title=title,
        reference_code=str(document.get("referenceId") or "") or None,
        package_year=package_year,
        published=iso_date("issued"),
        content_begin=iso_date("contentBegin"),
        content_end=iso_date("contentEnd"),
        license=str(license_name) if license_name else None,
        doi=doi_match.group(0).rstrip(".,;)") if doi_match else None,
        files=files,
        metadata_source="datastore_api_v7",
        metadata_api_url=api_url,
        profile_page_is_javascript_driven=True,
    )


def _year_from_iso(value: object) -> int | None:
    match = re.match(r"(\d{4})-", str(value or ""))
    return int(match.group(1)) if match else None


def select_main_file(package: DataPackageInfo) -> PackageFile:
    matches = [file for file in package.files if file.file_name == MAIN_FILE_NAME]
    if not matches:
        raise ScrapeError(
            f"{MAIN_FILE_NAME} was not found on the official package page: "
            f"{package.source_url}"
        )
    if len(matches) > 1:
        raise ScrapeError(
            f"multiple {MAIN_FILE_NAME} links found; refusing ambiguous download"
        )
    selected = matches[0]
    require_https_host(selected.url, PACKAGE_HOSTS, "data file link")
    if not selected.code or not selected.code.isdigit():
        raise ScrapeError(
            f"could not parse a numeric DataStore file code from {selected.url}"
        )
    return selected


def build_api_download_url(api_base: str, file_code: str) -> str:
    require_https_host(api_base, API_HOSTS, "DataStore API base")
    if not file_code.isdigit():
        raise SafetyError(f"file code must be numeric: {file_code!r}")
    return f"{api_base.rstrip('/')}/rest/DownloadFile/{file_code}?accessType=DOWNLOAD"


def verify_csv_header(path: Path) -> str:
    """Verify the official header, tolerating quoted fields and a UTF-8 BOM.

    The published ``Main_Data.csv`` quotes every header field
    (``"UnitCode","Year",...``), so a raw string comparison would reject a
    perfectly valid official download.
    """
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(csv.reader(handle), [])
    except OSError as exc:
        raise ScrapeError(f"cannot read downloaded file {path}: {exc}") from exc
    columns = [cell.strip() for cell in row]
    if columns != EXPECTED_CSV_COLUMNS:
        raise ScrapeError(
            "downloaded file does not have the expected official CSV header "
            f"{EXPECTED_CSV_HEADER!r}; got {','.join(columns)[:120]!r}"
        )
    return ",".join(columns)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HttpClient:
    """Small polite HTTP client with cache, rate limit, and bounded retries."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        retries: int = 3,
        backoff: float = 1.0,
        min_interval: float = 1.5,
        cache_ttl_hours: float = 24.0,
        refresh: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = max(1, retries)
        self.backoff = max(0.0, backoff)
        self.min_interval = max(0.0, min_interval)
        self.cache_ttl_seconds = max(0.0, cache_ttl_hours) * 3600
        self.refresh = refresh
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        self._last_request_at: float | None = None
        self._sleep = time.sleep
        self._monotonic = time.monotonic

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            LOG.debug("Rate limit: sleeping %.2fs", wait)
            self._sleep(wait)

    def _retry_delay(self, attempt: int, response: requests.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(60.0, max(0.0, float(retry_after)))
                except ValueError:
                    try:
                        when = parsedate_to_datetime(retry_after)
                        seconds = (when - datetime.now(timezone.utc)).total_seconds()
                        return min(60.0, max(0.0, seconds))
                    except (TypeError, ValueError):
                        pass
        base = self.backoff * (2 ** (attempt - 1))
        jitter = random.uniform(0, max(0.05, self.backoff / 2))
        return min(30.0, base + jitter)

    def _request_html(self, url: str) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                response = self.session.get(
                    url,
                    timeout=(self.connect_timeout, self.read_timeout),
                    allow_redirects=True,
                    stream=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = self._retry_delay(attempt, None)
                LOG.warning(
                    "Request failed (%s); retry %d/%d in %.1fs",
                    exc,
                    attempt + 1,
                    self.retries,
                    delay,
                )
                self._sleep(delay)
                continue
            finally:
                self._last_request_at = self._monotonic()

            if response.status_code == 200 or response.status_code == 304:
                return response
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self.retries:
                    response.close()
                    break
                delay = self._retry_delay(attempt, response)
                LOG.warning(
                    "HTTP %d from %s; retry %d/%d in %.1fs",
                    response.status_code,
                    url,
                    attempt + 1,
                    self.retries,
                    delay,
                )
                response.close()
                self._sleep(delay)
                continue
            status = response.status_code
            response.close()
            raise HttpError(f"HTTP {status} for {url}", status_code=status)

        if last_error:
            raise HttpError(f"request failed after {self.retries} attempts: {last_error}")
        raise HttpError(f"request failed after {self.retries} attempts for {url}")

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return (
            self.cache_dir / f"{digest}.body",
            self.cache_dir / f"{digest}.json",
        )

    def get_bytes(self, url: str) -> Fetched:
        body_path, meta_path = self._cache_paths(url)
        meta: dict[str, Any] | None = None
        if body_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = None

        now = time.time()
        if (
            not self.refresh
            and meta
            and body_path.exists()
            and now - float(meta.get("fetched_at_epoch", 0)) < self.cache_ttl_seconds
        ):
            LOG.info("Cache hit: %s", url)
            return Fetched(
                url=url,
                content=body_path.read_bytes(),
                status_code=int(meta.get("status_code", 200)),
                headers=dict(meta.get("headers", {})),
                fetched_at=str(meta.get("fetched_at", utc_now())),
                from_cache=True,
            )

        headers: dict[str, str] = {}
        if (
            not self.refresh
            and meta
            and body_path.exists()
            and meta.get("etag")
        ):
            headers["If-None-Match"] = str(meta["etag"])
        if (
            not self.refresh
            and meta
            and body_path.exists()
            and meta.get("last_modified")
        ):
            headers["If-Modified-Since"] = str(meta["last_modified"])
        if headers:
            self.session.headers.update(headers)
        try:
            response = self._request_html(url)
        finally:
            for key in headers:
                self.session.headers.pop(key, None)

        fetched_at = utc_now()
        if response.status_code == 304 and meta and body_path.exists():
            response.close()
            LOG.info("HTTP 304; using cached page: %s", url)
            meta["fetched_at_epoch"] = now
            meta["fetched_at"] = fetched_at
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return Fetched(
                url=url,
                content=body_path.read_bytes(),
                status_code=200,
                headers=dict(meta.get("headers", {})),
                fetched_at=fetched_at,
                from_cache=True,
            )

        content = response.content
        response.close()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(content)
        meta = {
            "url": url,
            "fetched_at": fetched_at,
            "fetched_at_epoch": now,
            "status_code": response.status_code,
            "headers": {
                key: response.headers.get(key, "")
                for key in ("Content-Type", "ETag", "Last-Modified")
            },
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "sha256": hashlib.sha256(content).hexdigest(),
            "cache_version": CACHE_VERSION,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        LOG.info("Fetched %s (%d bytes)", url, len(content))
        return Fetched(
            url=response.url,
            content=content,
            status_code=response.status_code,
            headers=dict(meta["headers"]),
            fetched_at=fetched_at,
        )

    def get_text(self, url: str) -> str:
        fetched = self.get_bytes(url)
        return fetched.content.decode("utf-8", errors="replace")

    def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int | None = None,
        refresh: bool = False,
    ) -> DownloadInfo:
        if destination.is_symlink():
            raise SafetyError(f"refusing to write through symlink: {destination}")
        if destination.exists() and not refresh:
            verify_csv_header(destination)
            size = destination.stat().st_size
            if expected_size and size > 0:
                ratio = size / expected_size
                if ratio < 0.5 or ratio > 1.5:
                    raise ScrapeError(
                        f"existing {destination.name} is {size} bytes but the "
                        f"package page estimates {expected_size}; use --refresh "
                        "to replace a partial or stale download"
                    )
            LOG.info("Using existing download: %s (%d bytes)", destination, size)
            return DownloadInfo(
                path=destination,
                bytes_written=size,
                sha256=sha256_file(destination),
                url=url,
                from_existing=True,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        if part.exists():
            part.unlink()
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                response = self.session.get(
                    url,
                    headers={
                        "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8"
                    },
                    timeout=(self.connect_timeout, self.read_timeout),
                    allow_redirects=True,
                    stream=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = self._retry_delay(attempt, None)
                LOG.warning(
                    "Download request failed (%s); retry %d/%d in %.1fs",
                    exc,
                    attempt + 1,
                    self.retries,
                    delay,
                )
                self._sleep(delay)
                continue
            finally:
                self._last_request_at = self._monotonic()

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self.retries:
                    response.close()
                    break
                delay = self._retry_delay(attempt, response)
                LOG.warning(
                    "HTTP %d from download; retry %d/%d in %.1fs",
                    response.status_code,
                    attempt + 1,
                    self.retries,
                    delay,
                )
                response.close()
                self._sleep(delay)
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise HttpError(
                    f"HTTP {status} while downloading {url}", status_code=status
                )

            final_host = hostname(response.url)
            if not is_official_nps_host(final_host):
                response.close()
                raise SafetyError(
                    f"download redirected to a non-official host: {response.url}"
                )

            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "xhtml" in content_type:
                response.close()
                raise ScrapeError(
                    f"official download returned HTML instead of CSV (Content-Type: "
                    f"{content_type}); likely an error or login page"
                )

            content_length = response.headers.get("Content-Length")
            content_encoding = response.headers.get("Content-Encoding", "identity").lower()
            expected_length = (
                int(content_length)
                if content_length
                and content_length.isdigit()
                and content_encoding in ("", "identity")
                else None
            )
            digest = hashlib.sha256()
            written = 0
            try:
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise ScrapeError(
                                f"download exceeded safety limit of "
                                f"{MAX_DOWNLOAD_BYTES} bytes"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
            except ScrapeError:
                response.close()
                part.unlink(missing_ok=True)
                raise
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                response.close()
                part.unlink(missing_ok=True)
                if attempt >= self.retries:
                    break
                delay = self._retry_delay(attempt, None)
                LOG.warning(
                    "Download interrupted (%s); retry %d/%d in %.1fs",
                    exc,
                    attempt + 1,
                    self.retries,
                    delay,
                )
                self._sleep(delay)
                continue
            finally:
                response.close()

            if expected_length is not None and written != expected_length:
                part.unlink(missing_ok=True)
                last_error = ScrapeError(
                    f"truncated download: wrote {written} of {expected_length} bytes"
                )
                if attempt >= self.retries:
                    break
                delay = self._retry_delay(attempt, None)
                LOG.warning("%s; retrying in %.1fs", last_error, delay)
                self._sleep(delay)
                continue

            if expected_size is not None and written > 0:
                ratio = written / expected_size
                if ratio < 0.5 or ratio > 1.5:
                    part.unlink(missing_ok=True)
                    raise ScrapeError(
                        "downloaded size "
                        f"{written} differs too much from the package-page estimate "
                        f"{expected_size} (ratio {ratio:.2f}); refusing incomplete data"
                    )

            # Validate the temporary file before replacing any existing good file.
            try:
                verify_csv_header(part)
            except ScrapeError:
                part.unlink(missing_ok=True)
                raise
            os.replace(part, destination)
            LOG.info(
                "Downloaded %s (%d bytes, sha256=%s)",
                url,
                written,
                digest.hexdigest(),
            )
            return DownloadInfo(
                path=destination,
                bytes_written=written,
                sha256=digest.hexdigest(),
                url=url,
            )

        if part.exists():
            part.unlink()
        if last_error:
            raise HttpError(f"download failed after {self.retries} attempts: {last_error}")
        raise HttpError(f"download failed after {self.retries} attempts: {url}")


class RobotsPolicy:
    """Check robots.txt for HTML page paths (not the single-file API request)."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client
        self._parsers: dict[str, RobotFileParser | None] = {}
        self.notes: dict[str, str] = {}

    def _parser(self, scheme_and_host: str) -> RobotFileParser | None:
        if scheme_and_host in self._parsers:
            return self._parsers[scheme_and_host]
        robots_url = f"{scheme_and_host}/robots.txt"
        parser: RobotFileParser | None = None
        try:
            text = self.client.get_text(robots_url)
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(text.splitlines())
            self.notes[scheme_and_host] = "robots.txt loaded"
        except HttpError as exc:
            if exc.status_code in (401, 403):
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(["User-agent: *", "Disallow: /"])
                self.notes[scheme_and_host] = (
                    f"robots.txt returned HTTP {exc.status_code}; failing closed"
                )
            elif exc.status_code == 404:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse([])
                self.notes[scheme_and_host] = "robots.txt not found; treating as allowed"
            else:
                parser = None
                self.notes[scheme_and_host] = (
                    f"robots.txt unavailable ({exc}); proceeding cautiously"
                )
        except (ScrapeError, OSError, UnicodeError) as exc:
            parser = None
            self.notes[scheme_and_host] = (
                f"robots.txt check failed ({exc}); proceeding cautiously"
            )
        self._parsers[scheme_and_host] = parser
        LOG.info("%s: %s", robots_url, self.notes[scheme_and_host])
        return parser

    def check(self, url: str, user_agent_token: str = "nps-visitation-toolkit") -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SafetyError(f"unsupported URL for robots check: {url}")
        origin = f"https://{parsed.hostname}"
        parser = self._parser(origin)
        if parser is not None and not parser.can_fetch(user_agent_token, url):
            raise SafetyError(
                f"robots.txt disallows automated access to {url}; refusing to fetch it"
            )
        delay = parser.crawl_delay(user_agent_token) if parser is not None else None
        if delay:
            self.client.min_interval = max(self.client.min_interval, float(delay))
            LOG.info("Honoring robots.txt Crawl-delay of %.1fs for %s", delay, origin)


def validate_page(page: NpsPageInfo) -> None:
    if "Visitor Use" not in f"{page.title} {page.h1}":
        raise ScrapeError(
            "the fetched page does not look like the official NPS Visitor Use "
            f"page (title={page.title!r}, h1={page.h1!r})"
        )
    if page.at_a_glance_year is None:
        raise ScrapeError(
            "could not determine the latest finalized year from the NPS page; "
            "the page structure may have changed"
        )
    if not page.portal_links:
        LOG.warning("No IRMA Stats portal link was discovered on the NPS page")
    if page.dashboard_is_javascript_driven:
        LOG.info(
            "Dashboard is a JavaScript/Power BI embed; its on-screen table is "
            "NOT scraped. Using the official downloadable data package instead."
        )
    if page.static_dashboard_table_found:
        LOG.warning(
            "A static table with recreation visits was found; the script still "
            "uses the official data package, not page-scraped tables."
        )


def validate_package(package: DataPackageInfo, page: NpsPageInfo) -> None:
    if "Visitor Use Statistics Data Package" not in package.title:
        raise ScrapeError(
            f"unexpected package page title: {package.title!r}"
        )
    if not package.content_end:
        raise ScrapeError("package page has no Content End Date")
    end_year = int(package.content_end[:4])
    if page.at_a_glance_year is not None and end_year != page.at_a_glance_year:
        if end_year < page.at_a_glance_year:
            raise ScrapeError(
                "the configured data package is older than the latest finalized "
                f"year on the NPS page ({end_year} < {page.at_a_glance_year}). "
                "Find the current package reference on https://irma.nps.gov/DataStore/ "
                "and pass it with --package-profile-url."
            )
        LOG.warning(
            "Data package (%d) is newer than the NPS page at-a-glance year (%d); "
            "continuing with the verified package",
            end_year,
            page.at_a_glance_year,
        )
    if not package.license:
        LOG.warning("No license was found on the package page; review terms manually")
    elif "creative commons zero" not in package.license.lower() and "public domain" not in package.license.lower():
        LOG.warning("Unexpected package license: %s", package.license)
    if not package.reference_code:
        LOG.warning("Package reference code was not parsed")


def verify_existing_download(
    destination: Path,
    metadata_path: Path,
    package: DataPackageInfo,
    selected: PackageFile,
) -> None:
    """Refuse to reuse a CSV that is not tied to the currently verified package."""
    if destination.is_symlink():
        raise SafetyError(f"refusing to use symlinked data file: {destination}")
    verify_csv_header(destination)
    if not metadata_path.exists():
        raise ScrapeError(
            f"{destination} already exists but {metadata_path} is missing, so its "
            "provenance cannot be verified. Move it aside or use --refresh."
        )
    try:
        previous = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScrapeError(
            f"cannot verify existing download provenance ({metadata_path}): {exc}; "
            "use --refresh to re-download"
        ) from exc
    previous_file = previous.get("selected_file", {}) if isinstance(previous, dict) else {}
    previous_package = previous.get("data_package", {}) if isinstance(previous, dict) else {}
    if previous_file.get("code") != selected.code:
        raise ScrapeError(
            f"existing {destination.name} came from a different DataStore file "
            f"({previous_file.get('code')} != {selected.code}); use --refresh"
        )
    if previous_package.get("reference_code") != package.reference_code:
        raise ScrapeError(
            f"existing {destination.name} came from a different package reference "
            f"({previous_package.get('reference_code')} != {package.reference_code}); "
            "use --refresh"
        )
    previous_sha = previous.get("download", {}).get("sha256")
    current_sha = sha256_file(destination)
    if previous_sha and previous_sha != current_sha:
        raise ScrapeError(
            f"existing {destination.name} does not match its recorded checksum; "
            "use --refresh to replace it"
        )
    LOG.info(
        "Existing CSV matches verified package %s and provenance metadata",
        package.reference_code,
    )


def page_info_dict(page: NpsPageInfo) -> dict[str, Any]:
    return {
        "source_url": page.source_url,
        "title": page.title,
        "h1": page.h1,
        "description": page.description,
        "last_updated": page.last_updated,
        "last_updated_text": page.last_updated_text,
        "at_a_glance_year": page.at_a_glance_year,
        "headline_recreation_visits_millions_rounded": page.headline_recreation_visits_millions,
        "units_reporting": page.units_reporting,
        "units_total": page.units_total,
        "dashboard_platform": page.dashboard_platform,
        "dashboard_is_javascript_driven": page.dashboard_is_javascript_driven,
        "static_dashboard_table_found": page.static_dashboard_table_found,
        "latest_abstract_year": page.latest_abstract_year,
        "limitations_noted": page.limitations_noted,
        "portal_links": [asdict(link) for link in page.portal_links],
        "data_download_links_on_page": [asdict(link) for link in page.data_download_links],
        "package_profile_links_on_page": [asdict(link) for link in page.package_profile_links],
        "dashboard_links": [asdict(link) for link in page.dashboard_links],
    }


def package_info_dict(package: DataPackageInfo) -> dict[str, Any]:
    data = asdict(package)
    return data


def write_source_metadata(
    path: Path,
    *,
    page: NpsPageInfo,
    package: DataPackageInfo,
    selected: PackageFile,
    download_url: str,
    download: DownloadInfo,
    robots_notes: Mapping[str, str],
    csv_header: str,
) -> None:
    citation_year = package.published[:4] if package.published else "n.d."
    citation = (
        f"Ziesler, P. {citation_year}. NPS Visitor Use Statistics Data Package, "
        f"{package.package_year or 'unknown'}. National Park Service. Fort Collins CO."
    )
    if package.doi:
        citation = f"{citation} https://doi.org/{package.doi}"
    payload = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "tool": TOOL,
        "source_page": page_info_dict(page),
        "data_package": package_info_dict(package),
        "selected_file": {
            **asdict(selected),
            "datastore_api_download_url": download_url,
        },
        "download": {
            "path": str(download.path),
            "bytes": download.bytes_written,
            "sha256": download.sha256,
            "from_existing_file": download.from_existing,
            "verified_header": csv_header,
        },
        "method_note": (
            "NPS page inspected with Requests + BeautifulSoup. The Power BI "
            "dashboard is JavaScript-driven and was not scraped. Exactly one "
            "official CSV (Main_Data.csv) was retrieved from the official "
            "IRMA DataStore REST API. The package ends at the latest finalized "
            "calendar year; current-year preliminary data are excluded."
        ),
        "robots_notes": dict(robots_notes),
        "citation": citation,
        "license": package.license,
        "coverage": {
            "begin": package.content_begin,
            "end": package.content_end,
            "latest_finalized_year": int(package.content_end[:4])
            if package.content_end
            else None,
        },
        "access_date_utc": datetime.now(timezone.utc).date().isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    LOG.info("Wrote provenance metadata: %s", path)


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


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the official NPS Visitor Use Data page and IRMA data package, "
            "then download Main_Data.csv (and write provenance metadata)."
        )
    )
    parser.add_argument("--page-url", default=DEFAULT_PAGE_URL, help="official NPS HTML page")
    parser.add_argument(
        "--package-profile-url",
        default=DEFAULT_PACKAGE_URL,
        help="official IRMA DataStore package profile (update when NPS publishes a new year)",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="official NPS DataStore REST API base URL",
    )
    parser.add_argument("--outdir", type=Path, default=Path("data/raw"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument(
        "--contact-email",
        default=os.environ.get("NPS_CONTACT_EMAIL", ""),
        help="optional contact address appended to the descriptive User-Agent",
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=1.0)
    parser.add_argument(
        "--min-interval",
        type=float,
        default=1.5,
        help="minimum seconds between any two HTTP requests (default 1.5)",
    )
    parser.add_argument("--cache-ttl-hours", type=float, default=24.0)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="bypass cached HTML and re-download the CSV if it already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify sources and print the official download URL without writing data",
    )
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _profile_id(profile_url: str) -> str:
    match = re.search(r"/Reference/Profile/(\d+)", profile_url)
    if not match:
        raise SafetyError(
            f"cannot parse a numeric DataStore reference from {profile_url!r}"
        )
    return match.group(1)


def run(args: argparse.Namespace, client: HttpClient, robots: RobotsPolicy) -> int:
    require_https_host(args.page_url, PAGE_HOSTS, "NPS page URL")
    require_https_host(args.package_profile_url, PACKAGE_HOSTS, "IRMA package URL")
    require_https_host(args.api_base, API_HOSTS, "DataStore API base")

    robots.check(args.page_url)
    robots.check(args.package_profile_url)

    page_fetched = client.get_bytes(args.page_url)
    if not is_official_nps_host(hostname(page_fetched.url)):
        raise SafetyError(f"NPS page redirected to a non-official host: {page_fetched.url}")
    page = parse_nps_page(page_fetched.content, args.page_url)
    validate_page(page)
    LOG.info(
        "NPS page: %s | last updated %s | at-a-glance year %s",
        page.title,
        page.last_updated or "unknown",
        page.at_a_glance_year,
    )
    if page.units_reporting is not None:
        LOG.info(
            "Reporting units on page: %s of %s", page.units_reporting, page.units_total
        )

    package_fetched = client.get_bytes(args.package_profile_url)
    if not is_official_nps_host(hostname(package_fetched.url)):
        raise SafetyError(
            f"package page redirected to a non-official host: {package_fetched.url}"
        )
    package = parse_data_package(package_fetched.content, args.package_profile_url)
    if not package.files:
        # The public profile page is an ExtJS shell: the static HTML has no file
        # table. Read the same public metadata from the documented DataStore API
        # instead of pretending the JavaScript-rendered page was scraped.
        profile_id = _profile_id(args.package_profile_url)
        api_url = f"{args.api_base.rstrip('/')}/rest/Profile/{profile_id}"
        require_https_host(api_url, API_HOSTS, "DataStore profile API URL")
        LOG.info(
            "Profile page is JavaScript-driven (no static file table); reading "
            "official DataStore API metadata instead: %s",
            api_url,
        )
        api_fetched = client.get_bytes(api_url)
        package = parse_package_json(
            api_fetched.content,
            profile_url=args.package_profile_url,
            api_url=api_url,
        )
    validate_package(package, page)
    selected = select_main_file(package)
    api_url = build_api_download_url(args.api_base, selected.code or "")
    LOG.info(
        "Verified official package: %s (Reference %s), published %s, coverage %s to %s",
        package.title,
        package.reference_code or "unknown",
        package.published or "unknown",
        package.content_begin or "unknown",
        package.content_end or "unknown",
    )
    LOG.info(
        "Selected only %s (%s); skipping state-level and metadata files",
        selected.file_name,
        selected.size_text or "size unknown",
    )
    LOG.info("Official DataStore file link: %s", selected.url)
    LOG.info("Single-file API download: %s", api_url)

    if args.dry_run:
        LOG.info(
            "Dry run requested: no data files written and no CSV download attempted "
            "(HTML/robots responses may be cached)."
        )
        return 0

    args.outdir.mkdir(parents=True, exist_ok=True)
    destination = args.outdir / MAIN_FILE_NAME
    metadata_path = args.outdir / "source_metadata.json"
    if destination.exists() and not args.refresh:
        verify_existing_download(destination, metadata_path, package, selected)
    download = client.download(
        api_url,
        destination,
        expected_size=selected.size_bytes_estimate,
        refresh=args.refresh,
    )
    header = verify_csv_header(destination)
    if download.from_existing:
        LOG.info("Existing CSV verified; provenance metadata will be refreshed.")
    write_source_metadata(
        metadata_path,
        page=page,
        package=package,
        selected=selected,
        download_url=api_url,
        download=download,
        robots_notes=robots.notes,
        csv_header=header,
    )
    LOG.info("Done. Clean with: python -m scraper.clean_visitation --input %s", destination)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    client: HttpClient | None = None,
    robots: RobotsPolicy | None = None,
) -> int:
    args = _parse_args(argv)
    try:
        configure_logging(args.verbose, args.log_file)
    except OSError as exc:
        print(f"ERROR: cannot open log file: {exc}", file=sys.stderr)
        return 2

    if re.search(r"[\r\n]", args.contact_email):
        LOG.error("--contact-email must not contain newlines")
        return 2

    user_agent = DEFAULT_USER_AGENT
    if args.contact_email:
        user_agent = f"{user_agent} contact:{args.contact_email.strip()}"

    if client is None:
        client = HttpClient(
            args.cache_dir,
            user_agent=user_agent,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            retries=args.retries,
            backoff=args.backoff,
            min_interval=args.min_interval,
            cache_ttl_hours=args.cache_ttl_hours,
            refresh=args.refresh,
        )
    if robots is None:
        robots = RobotsPolicy(client)

    try:
        return run(args, client, robots)
    except SafetyError as exc:
        LOG.error("Refusing request: %s", exc)
        return 2
    except ScrapeError as exc:
        LOG.error("Scrape failed: %s", exc)
        LOG.error(
            "No data file was written by this failed run. If network access is "
            "blocked, run the downloader in an environment that can reach "
            "irma.nps.gov and irmaservices.nps.gov; do not use fixture data as real data."
        )
        return 3
    except OSError as exc:
        LOG.error("Filesystem error: %s", exc)
        return 3
    except KeyboardInterrupt:
        LOG.error("Interrupted; partial .part files are removed by the downloader.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
