# Contributing to National Park Visitation Toolkit

Thank you for your interest in contributing to this project. We welcome improvements to the data pipeline, scraper robustness, analysis notebooks, and test coverage.

## Code of Conduct

All contributors and participants are expected to follow the project Code of Conduct detailed in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Getting Started

1. Fork the repository on GitHub.
2. Clone your fork locally:
   ```bash
   git clone https://github.com/<your-username>/national-park-visitation-toolkit.git
   cd national-park-visitation-toolkit
   ```
3. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e .
   ```
4. Create a working branch for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Guidelines

### Code Style and Standards
* Follow standard Python PEP 8 conventions.
* Add type annotations to functions and public methods.
* Keep modules focused and testable.
* Do not introduce heavy dependencies unless strictly necessary.

### Testing
All changes must pass the test suite before submitting a pull request.
Run the tests locally:
```bash
python -m pytest
```
When introducing new functionality or fixing a bug, add corresponding unit tests under the `tests/` directory.

### Offline and Polite Scraping
* Tests must run offline and must never send network requests to live NPS servers.
* Use existing fixtures in `tests/fixtures/` or add minimal synthetic fixtures.
* The scraper must respect site terms, robots directives, and rate limits.

## Submitting Pull Requests

1. Commit your changes with clear and concise commit messages.
2. Push your branch to your fork on GitHub.
3. Open a pull request against the `main` branch.
4. Fill out the provided pull request template explaining your changes and verification steps.
5. Ensure continuous integration checks pass.
