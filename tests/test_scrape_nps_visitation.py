from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scraper.scrape_nps_visitation import (
    DEFAULT_API_BASE,
    DownloadInfo,
    Fetched,
    HttpClient,
    HttpError,
    RobotsPolicy,
    SafetyError,
    ScrapeError,
    build_api_download_url,
    main,
    parse_data_package,
    parse_nps_page,
    parse_package_json,
    select_main_file,
    verify_csv_header,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE_URL = "https://www.nps.gov/subjects/socialscience/visitor-use-statistics-dashboard.htm"
PACKAGE_URL = "https://irma.nps.gov/DataStore/Reference/Profile/2317666"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture()
def page_html() -> bytes:
    return fixture("nps_visitor_use_data_page.html")


@pytest.fixture()
def package_html() -> bytes:
    return fixture("irma_data_package_2025.html")


@pytest.fixture()
def csv_bytes() -> bytes:
    return fixture("main_data_synthetic.csv")


def test_parse_official_page(page_html: bytes) -> None:
    page = parse_nps_page(page_html, PAGE_URL)
    assert page.title.startswith("Visitor Use Data")
    assert page.h1 == "Visitor Use Data"
    assert page.last_updated == "2026-03-26"
    assert page.at_a_glance_year == 2025
    assert page.headline_recreation_visits_millions == 323.0
    assert page.units_reporting == 406
    assert page.units_total == 433
    assert page.dashboard_platform == "Microsoft Power BI"
    assert page.dashboard_is_javascript_driven is True
    assert page.static_dashboard_table_found is False
    assert page.latest_abstract_year == 2024  # abstract lags the 2025 data package
    assert "Current Year Data Warning" in page.limitations_noted
    assert any("irma.nps.gov/Stats" in link.url for link in page.portal_links)
    assert any("DownloadFile/722329" in link.url for link in page.data_download_links)
    assert any("powerbigov" in link.url for link in page.dashboard_links)


def test_parse_package(page_html: bytes, package_html: bytes) -> None:
    page = parse_nps_page(page_html, PAGE_URL)
    package = parse_data_package(package_html, PACKAGE_URL)
    assert package.title == "NPS Visitor Use Statistics Data Package, 2025"
    assert package.reference_code == "2317666"
    assert package.package_year == 2025
    assert page.at_a_glance_year == package.package_year
    assert package.published == "2026-03-26"
    assert package.content_begin == "1979-01-01"
    assert package.content_end == "2025-12-31"
    assert package.doi == "10.57830/2317666"
    assert package.license and "Creative Commons Zero" in package.license
    assert len(package.files) == 3
    main_file = select_main_file(package)
    assert main_file.file_name == "Main_Data.csv"
    assert main_file.code == "756959"
    assert main_file.size_text == "68.9 MB"
    assert main_file.size_bytes_estimate and 70_000_000 < main_file.size_bytes_estimate < 75_000_000


def test_verify_csv_header_accepts_quoted_official_header(tmp_path: Path) -> None:
    quoted = tmp_path / "quoted.csv"
    quoted.write_text(
        '\ufeff"UnitCode","Year","Month","Statistic","Value"\r\n"ABLI",1979,1,"TRV",1586\r\n',
        encoding="utf-8",
    )
    assert verify_csv_header(quoted) == "UnitCode,Year,Month,Statistic,Value"


def test_parse_package_json_profile() -> None:
    package = parse_package_json(
        fixture("irma_profile_2317666.json"),
        PACKAGE_URL,
        DEFAULT_API_BASE + "/rest/Profile/2317666",
    )
    assert package.title == "NPS Visitor Use Statistics Data Package, 2025"
    assert package.reference_code == "2317666"
    assert package.package_year == 2025
    assert package.published == "2026-03-26"
    assert package.content_begin == "1979-01-01"
    assert package.content_end == "2025-12-31"
    assert package.doi == "10.57830/2317666"
    assert package.license and "Creative Commons Zero" in package.license
    assert package.metadata_source == "datastore_api_v7"
    assert package.profile_page_is_javascript_driven is True
    main_file = select_main_file(package)
    assert main_file.file_name == "Main_Data.csv"
    assert main_file.code == "756959"


def test_select_main_file_requires_exact_file(package_html: bytes) -> None:
    package = parse_data_package(
        package_html.replace(b"Main_Data.csv", b"Other_Data.csv"), PACKAGE_URL
    )
    with pytest.raises(ScrapeError, match="Main_Data.csv"):
        select_main_file(package)


def test_build_api_download_url() -> None:
    assert (
        build_api_download_url(DEFAULT_API_BASE, "756959")
        == "https://irmaservices.nps.gov/datastore/v7/rest/DownloadFile/756959?accessType=DOWNLOAD"
    )
    with pytest.raises(SafetyError):
        build_api_download_url("https://example.com/datastore", "756959")
    with pytest.raises(SafetyError):
        build_api_download_url(DEFAULT_API_BASE, "../../etc/passwd")


def test_verify_csv_header(tmp_path: Path) -> None:
    good = tmp_path / "good.csv"
    good.write_text("UnitCode,Year,Month,Statistic,Value\nAAAA,2024,1,TRV,1\n")
    assert verify_csv_header(good) == "UnitCode,Year,Month,Statistic,Value"

    bad = tmp_path / "bad.csv"
    bad.write_text("<html>not data</html>\n")
    with pytest.raises(ScrapeError, match="expected official CSV header"):
        verify_csv_header(bad)


class FakeRobots:
    def __init__(self) -> None:
        self.notes = {"https://www.nps.gov": "test", "https://irma.nps.gov": "test"}

    def check(self, url: str, user_agent_token: str = "nps-visitation-toolkit") -> None:
        return None


class FakeClient:
    def __init__(self, pages: dict[str, bytes], csv_bytes: bytes) -> None:
        self.pages = pages
        self.csv_bytes = csv_bytes
        self.download_calls: list[str] = []

    def get_bytes(self, url: str) -> Fetched:
        if url not in self.pages:
            raise ScrapeError(f"unexpected test URL: {url}")
        content = self.pages[url]
        return Fetched(
            url=url,
            content=content,
            status_code=200,
            headers={"Content-Type": "text/html"},
            fetched_at="2026-10-07T00:00:00Z",
        )

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).content.decode("utf-8")

    def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int | None = None,
        refresh: bool = False,
    ) -> DownloadInfo:
        self.download_calls.append(url)
        if destination.exists() and not refresh:
            data = destination.read_bytes()
            return DownloadInfo(
                path=destination,
                bytes_written=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                url=url,
                from_existing=True,
            )
        destination.write_bytes(self.csv_bytes)
        return DownloadInfo(
            path=destination,
            bytes_written=len(self.csv_bytes),
            sha256=hashlib.sha256(self.csv_bytes).hexdigest(),
            url=url,
        )


def test_main_downloads_exactly_one_official_csv(
    tmp_path: Path, page_html: bytes, package_html: bytes, csv_bytes: bytes
) -> None:
    client = FakeClient({PAGE_URL: page_html, PACKAGE_URL: package_html}, csv_bytes)
    outdir = tmp_path / "raw"
    rc = main(
        [
            "--outdir",
            str(outdir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--package-profile-url",
            PACKAGE_URL,
        ],
        client=client,
        robots=FakeRobots(),
    )
    assert rc == 0
    data_path = outdir / "Main_Data.csv"
    metadata_path = outdir / "source_metadata.json"
    assert data_path.read_bytes() == csv_bytes
    assert len(client.download_calls) == 1
    assert client.download_calls[0].endswith("/rest/DownloadFile/756959?accessType=DOWNLOAD")

    metadata = json.loads(metadata_path.read_text())
    assert metadata["data_package"]["reference_code"] == "2317666"
    assert metadata["data_package"]["content_end"] == "2025-12-31"
    assert metadata["selected_file"]["file_name"] == "Main_Data.csv"
    assert metadata["selected_file"]["code"] == "756959"
    assert metadata["download"]["sha256"] == hashlib.sha256(csv_bytes).hexdigest()
    assert metadata["source_page"]["latest_abstract_year"] == 2024
    assert metadata["source_page"]["dashboard_is_javascript_driven"] is True
    assert "not scraped" in metadata["method_note"]
    assert "CC0" in (metadata["license"] or "") or "Creative Commons" in (metadata["license"] or "")
    assert not (outdir / "Main_State_Data.csv").exists()
    assert not (outdir / "NPS_VUStats_Data_2025_metadata.xml").exists()


def test_falls_back_to_api_when_profile_page_is_javascript_shell(
    tmp_path: Path, page_html: bytes, csv_bytes: bytes
) -> None:
    shell = (
        b"<!DOCTYPE html><html><head><title>NPS DataStore</title></head>"
        b"<body><div id='app'></div></body></html>"
    )
    api_url = DEFAULT_API_BASE + "/rest/Profile/2317666"
    client = FakeClient(
        {
            PAGE_URL: page_html,
            PACKAGE_URL: shell,
            api_url: fixture("irma_profile_2317666.json"),
        },
        csv_bytes,
    )
    outdir = tmp_path / "raw"
    rc = main(
        [
            "--outdir",
            str(outdir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--package-profile-url",
            PACKAGE_URL,
        ],
        client=client,
        robots=FakeRobots(),
    )
    assert rc == 0
    metadata = json.loads((outdir / "source_metadata.json").read_text())
    assert metadata["data_package"]["metadata_source"] == "datastore_api_v7"
    assert metadata["data_package"]["profile_page_is_javascript_driven"] is True
    assert metadata["selected_file"]["code"] == "756959"
    assert len(client.download_calls) == 1


def test_dry_run_writes_no_data(
    tmp_path: Path, page_html: bytes, package_html: bytes, csv_bytes: bytes
) -> None:
    client = FakeClient({PAGE_URL: page_html, PACKAGE_URL: package_html}, csv_bytes)
    outdir = tmp_path / "raw"
    rc = main(
        [
            "--outdir",
            str(outdir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--package-profile-url",
            PACKAGE_URL,
            "--dry-run",
        ],
        client=client,
        robots=FakeRobots(),
    )
    assert rc == 0
    assert not outdir.exists()
    assert client.download_calls == []


def test_aborts_when_nps_page_year_is_newer_than_package(
    tmp_path: Path, page_html: bytes, package_html: bytes, csv_bytes: bytes
) -> None:
    newer_page = page_html.replace(b"2025 Visitation At-A-Glance", b"2026 Visitation At-A-Glance")
    newer_page = newer_page.replace(b"calendar year 2025", b"calendar year 2026")
    client = FakeClient({PAGE_URL: newer_page, PACKAGE_URL: package_html}, csv_bytes)
    outdir = tmp_path / "raw"
    rc = main(
        [
            "--outdir",
            str(outdir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--package-profile-url",
            PACKAGE_URL,
        ],
        client=client,
        robots=FakeRobots(),
    )
    assert rc == 3
    assert not (outdir / "Main_Data.csv").exists()
    assert client.download_calls == []


def test_reuses_existing_file_only_when_provenance_matches(
    tmp_path: Path, page_html: bytes, package_html: bytes, csv_bytes: bytes
) -> None:
    client = FakeClient({PAGE_URL: page_html, PACKAGE_URL: package_html}, csv_bytes)
    args = [
        "--outdir",
        str(tmp_path / "raw"),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--package-profile-url",
        PACKAGE_URL,
    ]
    assert main(args, client=client, robots=FakeRobots()) == 0
    assert main(args, client=client, robots=FakeRobots()) == 0
    assert (tmp_path / "raw" / "Main_Data.csv").read_bytes() == csv_bytes


def test_refuses_existing_file_without_provenance(
    tmp_path: Path, page_html: bytes, package_html: bytes, csv_bytes: bytes
) -> None:
    outdir = tmp_path / "raw"
    outdir.mkdir()
    (outdir / "Main_Data.csv").write_bytes(csv_bytes)
    client = FakeClient({PAGE_URL: page_html, PACKAGE_URL: package_html}, csv_bytes)
    rc = main(
        [
            "--outdir",
            str(outdir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--package-profile-url",
            PACKAGE_URL,
        ],
        client=client,
        robots=FakeRobots(),
    )
    assert rc == 3
    assert client.download_calls == []


def test_rejects_non_official_page_url(tmp_path: Path) -> None:
    rc = main(
        ["--page-url", "https://example.com/visitation", "--outdir", str(tmp_path / "raw")],
        client=FakeClient({}, b""),
        robots=FakeRobots(),
    )
    assert rc == 2


class FakeResponse:
    def __init__(self, status_code: int, content: bytes, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.url = "https://www.nps.gov/test"
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int = 8192) -> object:
        yield self.content


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls = 0

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls += 1
        response = self.responses.pop(0)
        response.url = url
        return response


def make_client(tmp_path: Path, session: FakeSession) -> HttpClient:
    client = HttpClient(
        tmp_path / "cache",
        session=session,  # type: ignore[arg-type]
        retries=3,
        backoff=0.01,
        min_interval=0.0,
    )
    client._sleep = lambda seconds: None  # type: ignore[method-assign]
    client._monotonic = lambda: 0.0  # type: ignore[method-assign]
    return client


def test_http_cache_avoids_second_request(tmp_path: Path) -> None:
    session = FakeSession([FakeResponse(200, b"<html>cached</html>")])
    client = make_client(tmp_path, session)
    first = client.get_bytes("https://www.nps.gov/test")
    second = client.get_bytes("https://www.nps.gov/test")
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.content == b"<html>cached</html>"
    assert session.calls == 1


def test_http_retries_with_backoff_on_503(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(503, b"busy", {"Retry-After": "0"}),
            FakeResponse(503, b"busy", {"Retry-After": "0"}),
            FakeResponse(200, b"<html>ok</html>"),
        ]
    )
    client = make_client(tmp_path, session)
    fetched = client.get_bytes("https://www.nps.gov/test")
    assert fetched.content == b"<html>ok</html>"
    assert session.calls == 3


def test_http_does_not_retry_404(tmp_path: Path) -> None:
    session = FakeSession([FakeResponse(404, b"missing")])
    client = make_client(tmp_path, session)
    with pytest.raises(HttpError) as exc:
        client.get_bytes("https://www.nps.gov/missing")
    assert exc.value.status_code == 404
    assert session.calls == 1


def test_robots_disallow_is_enforced(tmp_path: Path) -> None:
    class RobotsClient:
        def get_text(self, url: str) -> str:
            return "User-agent: *\nDisallow: /private/\n"

    policy = RobotsPolicy(RobotsClient())  # type: ignore[arg-type]
    with pytest.raises(SafetyError, match="robots.txt disallows"):
        policy.check("https://www.nps.gov/private/page.html")
    policy.check("https://www.nps.gov/public/page.html")


def test_download_rejects_and_removes_html_payload(tmp_path: Path) -> None:
    session = FakeSession(
        [FakeResponse(200, b"<html>not a csv</html>", {"Content-Type": "text/csv"})]
    )
    client = make_client(tmp_path, session)
    destination = tmp_path / "raw" / "Main_Data.csv"
    with pytest.raises(ScrapeError, match="expected official CSV header"):
        client.download(
            "https://irmaservices.nps.gov/datastore/v7/rest/DownloadFile/1?accessType=DOWNLOAD",
            destination,
        )
    assert not destination.exists()


def test_download_writes_csv_and_reuses_existing(tmp_path: Path) -> None:
    payload = b"UnitCode,Year,Month,Statistic,Value\nAAAA,2024,1,TRV,1\n"
    session = FakeSession([FakeResponse(200, payload, {"Content-Type": "text/csv"})])
    client = make_client(tmp_path, session)
    destination = tmp_path / "raw" / "Main_Data.csv"
    first = client.download(
        "https://irmaservices.nps.gov/datastore/v7/rest/DownloadFile/1?accessType=DOWNLOAD",
        destination,
    )
    assert destination.read_bytes() == payload
    assert first.from_existing is False
    assert first.bytes_written == len(payload)
    second = client.download(
        "https://irmaservices.nps.gov/datastore/v7/rest/DownloadFile/1?accessType=DOWNLOAD",
        destination,
    )
    assert second.from_existing is True
    assert session.calls == 1


def test_bad_refresh_keeps_previous_good_download(tmp_path: Path) -> None:
    good = b"UnitCode,Year,Month,Statistic,Value\nAAAA,2024,1,TRV,1\n"
    url = "https://irmaservices.nps.gov/datastore/v7/rest/DownloadFile/1?accessType=DOWNLOAD"
    destination = tmp_path / "raw" / "Main_Data.csv"
    client = make_client(tmp_path, FakeSession([FakeResponse(200, good, {"Content-Type": "text/csv"})]))
    client.download(url, destination)

    bad_client = make_client(
        tmp_path / "bad", FakeSession([FakeResponse(200, b"<html>error</html>", {"Content-Type": "text/csv"})])
    )
    with pytest.raises(ScrapeError):
        bad_client.download(url, destination, refresh=True)

    assert destination.read_bytes() == good
    assert not destination.with_name(destination.name + ".part").exists()


def test_malformed_or_empty_page_html_is_rejected(tmp_path: Path, csv_bytes: bytes) -> None:
    for payload in (b"", b"<html><body><h1>Broken", b"<html><title>Something else</title></html>"):
        client = FakeClient({PAGE_URL: payload}, csv_bytes)
        rc = main(
            [
                "--outdir",
                str(tmp_path / "raw"),
                "--cache-dir",
                str(tmp_path / "cache"),
            ],
            client=client,
            robots=FakeRobots(),
        )
        assert rc == 3
        assert not (tmp_path / "raw" / "Main_Data.csv").exists()


def test_empty_api_payload_is_rejected() -> None:
    with pytest.raises(ScrapeError, match="did not return JSON"):
        parse_package_json(b"", PACKAGE_URL, DEFAULT_API_BASE + "/rest/Profile/2317666")
    with pytest.raises(ScrapeError, match="unexpected JSON"):
        parse_package_json(b"[]", PACKAGE_URL, DEFAULT_API_BASE + "/rest/Profile/2317666")
