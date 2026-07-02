# Automation Test Stability Analytics

This project builds a practical analytics and ML workflow for CI test data. It combines synthetic test-log generation, automated ingestion into a SQLite database, a Streamlit dashboard, and ML-driven insights for flaky tests, duration drift, failure clustering, anomaly detection, and Jira defect mapping.

## Why this project exists

Modern CI pipelines generate large volumes of noisy test data. This project helps teams answer questions such as:

- Which tests are flaky or unstable?
- Which runs look abnormal?
- Which tests are becoming slower over time?
- Which failures are related and should be triaged together?
- Which Jira defects correspond to which failed test executions?

The result is a faster path from raw CI output to actionable debugging and prioritization.

## Key features

- Synthetic test-report generation for realistic Robot Framework-style XML and CI metadata
- Ingestion of test runs into SQLite for analysis and historical tracking
- A Streamlit dashboard for run health, drift, failure inspection, and defect mapping
- ML-focused insights for:
  - flakiness prediction
  - duration drift detection
  - test prioritization
  - failure clustering
  - anomaly detection
- Optional Jira integration for defect ingestion, mapping, and write-back

## Project structure

- [generate.py](generate.py) — creates synthetic CI runs and Robot Framework-compatible XML logs
- [config.py](config.py) — configuration for tests, failure behavior, dependencies, and run generation
- [pipeline.py](pipeline.py) — ingests run artifacts, builds the analytics database, and supports Jira defect mapping
- [dashboard.py](dashboard.py) — main Streamlit dashboard entry point
- [pages/2_ML_Insights.py](pages/2_ML_Insights.py) — ML insights page
- [ml_pipeline.py](ml_pipeline.py) — ML utilities and reporting helpers
- [schema.sql](schema.sql) — SQLite schema for runs, test results, defects, and mappings
- [design_doc.md](design_doc.md) — design notes for the synthetic dataset and ML approach
- [requirements.txt](requirements.txt) — Python dependencies

## Getting started

### Prerequisites

- Python 3.10+
- pip
- A virtual environment is recommended

### 1. Clone and install dependencies

```bash
git clone <your-repo-url>
cd Automation-Test-Stability-Analytics---HPE-CPP3-Jira-Defects-Mapping
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> Note: the embedding-based defect matching feature is optional but recommended. If you do not want to install the heavier semantic embedding stack, the pipeline can fall back to rule-based matching.

### 2. Generate sample CI data

```bash
python generate.py
```

This creates a set of synthetic run folders under [runs](runs) with XML reports and metadata.

### 3. Ingest the runs into the analytics database

```bash
python pipeline.py
```

This creates or updates [analytics.db](analytics.db) using the schema in [schema.sql](schema.sql).

### 4. Launch the dashboards

Run the main dashboard:

```bash
streamlit run dashboard.py
```

Run the ML-focused page separately:

```bash
streamlit run pages/2_ML_Insights.py -- --db ./analytics.db
```

## Usage examples

### Generate a custom synthetic dataset

```bash
python generate.py --output-dir ./data --num-runs 50 --start-date 2025-01-01 --interval 12 --seed 7 --team TeamBeta
```

### Ingest a different runs directory

```bash
python pipeline.py --runs-dir ./data --db ./teambeta.db
```

### Ingest Jira defects from file

```bash
python pipeline.py --ingest-jira ./defects.json
```

### Run defect-to-test mapping

```bash
python pipeline.py --map-defects
```

### Test Jira credentials without ingesting data

```bash
python pipeline.py --jira-test-connection
```

## Configuration notes

- [config.py](config.py) controls the synthetic data generation process, including test definitions, failure behavior, dependencies, and anomaly patterns.
- [schema.sql](schema.sql) defines the database structure used by the ingestion pipeline and dashboard.
- Jira integration requires environment variables or a local [.env](.env) file with values such as:

```bash
export JIRA_BASE_URL="https://your-org.atlassian.net"
export JIRA_EMAIL="your-email@company.com"
export JIRA_API_TOKEN="your-token"
```

## How the workflow fits together

1. Synthetic runs are generated from [generate.py](generate.py).
2. The pipeline ingests those runs into SQLite via [pipeline.py](pipeline.py).
3. The dashboard visualizes health, trends, and defect mappings.
4. The ML page surfaces predictive and clustering insights for test stability.

## Support and documentation

Useful references:

- [design_doc.md](design_doc.md) — explains the synthetic data design and ML strategy
- [schema.sql](schema.sql) — database design and table layout
- [requirements.txt](requirements.txt) — dependency list and environment guidance

If you run into issues, start by checking the project files above and the CLI help output:

```bash
python pipeline.py --help
```

## Contributing

Contributions are welcome. A good workflow is:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Open a pull request with a short explanation of the improvement

Please keep changes focused, document significant behavior changes, and update relevant docs when needed.

## Maintainers

This repository is intended as a reusable analytics and ML prototype for CI test stability. Contributions and feedback are welcome from anyone working on similar automation reliability problems.
