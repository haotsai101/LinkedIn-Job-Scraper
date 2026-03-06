# Dagster Integration for LinkedIn Job Scraper

This guide explains how to use Dagster to manage the CSV files as assets.

## Overview

The Dagster integration provides:
- **CSV Assets**: Each CSV file in `csv_files/` is defined as a Dagster asset
- **Asset Materialization**: Assets are materialized when CSV files are loaded
- **Lineage Tracking**: Track dependencies and data lineage
- **Centralized Data Management**: Organize and orchestrate data pipelines

## Project Initialization

The Dagster project has been initialized with the following structure:

```
.dagster_home/                # Dagster instance directory
  └── dagster.yaml           # Instance configuration (auto-created)
scripts/
  ├── __init__.py            # Package initialization
  ├── dagster_assets.py      # Asset definitions
  ├── definitions.py         # Dagster definitions
  └── ...                    # Other existing scripts
pyproject.toml               # Project configuration with Dagster metadata
.env.dagster                 # Environment variables (optional)
setup_dagster.sh             # Setup script
```

### Quick Setup

Run the provided setup script to initialize everything:

```bash
./setup_dagster.sh
```

This script will:
1. Create the `.dagster_home` directory
2. Set up the `dagster.yaml` configuration
3. Verify all dependencies are installed
4. Test that Dagster definitions load correctly
5. Print the command to start Dagster

## Available Assets

### Individual CSV Assets
Each CSV file is available as a separate asset:
- `benefits_csv`
- `companies_csv`
- `company_industries_csv`
- `company_specialities_csv`
- `employee_counts_csv`
- `industries_csv`
- `job_industries_csv`
- `job_postings_csv`
- `job_skills_csv`
- `salaries_csv`
- `skills_csv`

### Aggregated Assets
- `all_csv_files`: Dictionary containing all CSV files as DataFrames

## Running Dagster

### Option 1: Using Environment Variable (Recommended)

```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home dagster dev
```

Or use the provided `.env.dagster` file with a tool like `direnv` or `python-dotenv`.

### Option 2: Set DAGSTER_HOME Permanently (in `.zshrc`)

Add this to your `~/.zshrc`:
```bash
export DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home
```

Then reload your shell:
```bash
source ~/.zshrc
```

Now you can simply run:
```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
dagster dev
```

### Access the UI

Once the server starts, open your browser and navigate to:
```
http://localhost:3000
```

The Dagster web interface will show all your assets.

## Adding New Assets

To add new CSV files as assets:

1. Add a new function in `scripts/dagster_assets.py`:
```python
@asset
def your_csv_name_csv() -> pd.DataFrame:
    """Load your_csv_name.csv as a Dagster asset."""
    filepath = CSV_FOLDER / "your_csv_name.csv"
    logger.info(f"Loading your_csv_name from {filepath}")
    return pd.read_csv(filepath)
```

2. The new asset will automatically be available in Dagster without any other changes needed.

## Creating Ops and Jobs

You can create Dagster ops to process these assets:

```python
from dagster import op

@op
def process_job_postings(job_postings: pd.DataFrame):
    # Your processing logic
    return job_postings.describe()
```

Then create jobs that combine assets and ops.

## Project Structure

```
LinkedIn-Job-Scraper/
├── scripts/
│   ├── __init__.py
│   ├── dagster_assets.py      # Asset definitions
│   ├── definitions.py          # Dagster definitions
│   ├── create_db.py
│   ├── database_scripts.py
│   ├── fetch.py
│   └── helpers.py
├── csv_files/                  # Your CSV files
│   ├── benefits.csv
│   ├── companies.csv
│   └── ...
└── dagster.yaml               # Dagster configuration
```

## Next Steps

- Explore the Dagster UI to visualize your data assets
- Create ops to process and analyze the CSV data
- Set up schedules or sensors to automatically materialize assets
- Add more complex transformations and analyses
