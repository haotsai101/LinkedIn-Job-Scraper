# Dagster Quick Reference

## ⚡ Quick Start (3 Steps)

```bash
# Step 1: Set environment variable
export DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home

# Step 2: Navigate to project
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper

# Step 3: Start Dagster
dagster dev
```

Then open: `http://localhost:3000`

## 🎯 One-Liner

```bash
DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home dagster dev
```

## 🤖 Using the Setup Script

```bash
./setup_dagster.sh
```

## 📋 Available Assets

| Asset Name | Description |
|-----------|-------------|
| `benefits_csv` | Benefits data |
| `companies_csv` | Company information |
| `company_industries_csv` | Company industry associations |
| `company_specialities_csv` | Company specialties |
| `employee_counts_csv` | Employee count data |
| `industries_csv` | Industry data |
| `job_industries_csv` | Job industry associations |
| `job_postings_csv` | Job posting data |
| `job_skills_csv` | Job skills associations |
| `salaries_csv` | Salary data |
| `skills_csv` | Skills data |
| `all_csv_files` | All data as dictionary |

## 🔧 Common Commands

```bash
# Start development server
dagster dev

# List all assets
dagster asset list

# Materialize specific assets
dagster asset materialize --select job_postings_csv companies_csv

# Run in the background
nohup dagster dev &

# Stop all Dagster processes
pkill -f dagster
```

## 📂 Key Directories

```
.dagster_home/        # Dagster instance data
csv_files/            # Your data files
scripts/              # Python source code
```

## 💾 Configuration Files

- `pyproject.toml` - Project metadata
- `.dagster_home/dagster.yaml` - Dagster configuration
- `.env.dagster` - Environment variables (optional)
