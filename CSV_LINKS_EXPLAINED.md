# Understanding CSV Relationships in Dagster

## The Answer: CSVs ARE Linked by Foreign Keys!

Your CSV files **already have** the relationships you're asking about. They use **foreign keys** and **junction tables** to link data.

### Foreign Key Pattern

```
job_postings (main table)
├── company_id ──→ companies.company_id
├── job_id ──────→ benefits.job_id
├── job_id ──────→ salaries.job_id
└── job_id ──────→ job_skills.job_id & job_industries.job_id
```

## Examples of Relationships

### 1. Job → Company (One-to-Many)
```
job_postings.company_id REFERENCES companies.company_id

Example:
- Company ID 123 (Apple) has 10 job postings
- Each job_postings row with company_id=123 links to the same company record
```

### 2. Job → Skills (Many-to-Many via Junction Table)
```
job_postings.job_id → job_skills.job_id ← skills.skill_abr

Example:
- Job ID 456 requires: Python, SQL, AWS
- Three rows in job_skills table:
  - (job_id: 456, skill_abr: PYT)
  - (job_id: 456, skill_abr: SQL)
  - (job_id: 456, skill_abr: AWS)
- Join with skills.csv to get skill_name
```

### 3. Job → Industries (Many-to-Many via Junction Table)
```
job_postings.job_id → job_industries.job_id ← industries.industry_id

Example:
- Job ID 789 is in: Technology, Financial Services
- Two rows in job_industries table
```

### 4. Job → Salary (One-to-One or One-to-Many)
```
job_postings.job_id → salaries.job_id

Example:
- Job ID 101 has salary record with min/med/max
- Often one salary per job, but can have multiple salary versions
```

## Why They're Not "Linked" in Dagster (Until Now)

Previously, Dagster only had **independent assets** - each CSV was loaded separately with no relationships shown. Now we've added:

1. **Dependent Assets** - Assets that depend on each other
2. **Joined Datasets** - Pre-joined tables showing the relationships
3. **Aggregations** - Summary views across relationships

## New Linked Assets Available

### Direct Joins
- `jobs_with_companies` - Jobs enriched with company data
- `jobs_with_skills` - Jobs with all their required skills
- `jobs_with_industries` - Jobs with all their industries
- `jobs_with_salaries` - Jobs with salary ranges
- `companies_with_industries` - Companies with their industries

### Complete Views
- `complete_job_data` - Full enriched job data with companies, skills, and salaries

### Summaries
- `company_job_summary` - Jobs per company
- `skill_demand_summary` - Skills ranked by job demand
- `industry_job_summary` - Jobs per industry

## How to Use Linked Assets

### Step 1: Start Dagster
```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
DAGSTER_HOME=.dagster_home dagster dev
```

### Step 2: View Asset Lineage
1. Go to **Assets** tab
2. Click on any linked asset (e.g., `jobs_with_companies`)
3. You'll see its **dependencies** - which assets it uses
4. Click "View Lineage Graph" to see the relationships visually

### Step 3: Materialize Linked Assets
1. Click on `jobs_with_companies` in the UI
2. Click **Materialize**
3. Dagster automatically materializes dependencies first
4. You'll see the full join result

## Example: Following the Job → Company → Industries Path

```
jobs_with_companies materialized
  ├─ depends on: job_postings_csv
  └─ depends on: companies_csv
       └─ can then create: companies_with_industries
           └─ depends on: company_industries_csv
               └─ links back to: industries_csv
```

## The Data Flow

```
Raw CSVs → Load Assets → Join Assets → Enriched Data → Aggregations → Business Intelligence
   ↓            ↓              ↓              ↓              ↓                  ↓
job_       job_postings    jobs_with_   complete_      skill_demand_      Insights
postings   _csv            companies   job_data        summary
companies                  jobs_with_
job_skills                 skills
...                        jobs_with_
                          salaries
```

## Key Insights

1. **Foreign Keys Link the Data** - The CSVs use standard relational database patterns
2. **Junction Tables Handle Many-to-Many** - `job_skills` and `job_industries` link multiple items
3. **Dagster Shows the Graph** - Asset lineage makes relationships visible
4. **Materialized Assets Join the Data** - You can work with pre-joined, enriched data

## Next Steps

1. **Materialize** `jobs_with_companies` to see a job + company dataset
2. **Explore** `skill_demand_summary` to see top-demanded skills
3. **Query** `complete_job_data` for analysis across all dimensions
4. **Create** more ops/assets for your specific analysis needs

## For Database-Level Relationships

If you want **true database enforcement** with constraints, you could:
- Convert these CSVs to a SQLite/PostgreSQL database
- Use Dagster's `DuckDB` or SQL I/O manager
- Define foreign key constraints at the database level

See `DATABASE_SETUP.md` for those options (coming soon).
