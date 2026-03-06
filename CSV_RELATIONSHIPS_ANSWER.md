# Why Your CSVs Aren't "Linked" - And Now They Are! ✨

## Your Question
> "Why doesn't the csvs link to each other? The job_id, company_id, skill, and salary_id should link to each other"

## The Real Answer

### Part 1: They WERE Already Linked! 🎯
Your CSVs **ARE** linked using standard relational database patterns:
- **Foreign Keys** (FK) - IDs that reference other tables
- **Junction Tables** - Connect many-to-many relationships

### Part 2: Now They're Visibly Linked in Dagster! 🔗
We've created **new Dagster assets** that:
- Join related CSVs together
- Show dependencies in the UI
- Create enriched datasets automatically

## What Changed

### Added New File: `scripts/dagster_relationships.py`

**15 new assets** organized in 3 categories:

#### 1️⃣ Core Assets (Load raw CSVs) - 11 assets
- Same as before: `job_postings_csv`, `companies_csv`, `skills_csv`, etc.
- No changes, just organized

#### 2️⃣ Linked Assets (Show relationships) - 5 assets
These JOIN related tables together:

```
jobs_with_companies
├─ Joins: job_postings + companies
├─ Via: job_postings.company_id = companies.company_id
└─ Result: Jobs with company info

jobs_with_skills
├─ Joins: jobs + job_skills + skills
├─ Via: job_id and skill_abr foreign keys
└─ Result: Each job row repeated for each skill required

jobs_with_industries
├─ Joins: jobs + job_industries + industries
├─ Via: job_id and industry_id foreign keys
└─ Result: Each job row repeated for each industry

jobs_with_salaries
├─ Joins: jobs + salaries
├─ Via: job_id foreign key
└─ Result: Jobs with salary ranges

companies_with_industries
├─ Joins: companies + company_industries
├─ Via: company_id foreign key
└─ Result: Companies with their industries
```

#### 3️⃣ Complete View (Everything together) - 1 asset
```
complete_job_data
├─ Combines: jobs + companies + skills + salaries
└─ Result: Fully enriched job data
```

#### 4️⃣ Aggregations (Summary views) - 3 assets
```
company_job_summary
├─ Groups by: company
└─ Shows: How many jobs per company?

skill_demand_summary
├─ Groups by: skill
└─ Shows: Which skills are most demanded?

industry_job_summary
├─ Groups by: industry
└─ Shows: How many jobs per industry?
```

## How It Works in Dagster

### Before (Independent Assets):
```
job_postings_csv
companies_csv
skills_csv
(no relationship shown)
```

### After (Linked with Dependencies):
```
jobs_with_companies
├─ depends on → job_postings_csv
└─ depends on → companies_csv
      ↓ (materializes both first)
      ↓ joins them together
      ↓ produces: enriched dataset
```

### In Dagster UI:
1. **Assets** tab → See all assets
2. Click `jobs_with_companies` → See it depends on job_postings + companies
3. **Materialize** → Automatically:
   - Loads job_postings_csv
   - Loads companies_csv
   - Joins them
   - Stores result

## The Foreign Key Structure

### Simple (One-to-Many):
```
job_postings.company_id → companies.company_id
  (one company, many jobs)
```

### Complex (Many-to-Many):
```
job_postings.job_id → job_skills.job_id → skills.skill_abr
  (one job, many skills)
  (one skill, many jobs)
```

## Data Example

### Raw (Separate):
```
job_postings:
| job_id | company_id | title          |
|--------|-----------|-----------------|
| 1      | 100       | Software Eng   |
| 2      | 100       | Data Scientist |

companies:
| company_id | name   |
|-----------|--------|
| 100       | Google |
| 200       | Apple  |
```

### Joined (jobs_with_companies):
```
| job_id | company_id | title          | name   |
|--------|-----------|-----------------|--------|
| 1      | 100       | Software Eng   | Google |
| 2      | 100       | Data Scientist | Google |
```

## Files Updated/Created

### Code:
- `scripts/dagster_relationships.py` ✨ NEW
- `scripts/definitions.py` (updated to load relationships)

### Documentation:
- `CSV_RELATIONSHIPS.md` - ER diagram
- `CSV_LINKS_EXPLAINED.md` - Detailed guide
- `RELATIONSHIPS_SUMMARY.md` - Quick summary
- `RELATIONSHIPS_QUICK_REF.md` - Reference card
- `CSV_RELATIONSHIPS_ANSWER.md` ← You are here!

## How to Use It

### 1. Start Dagster:
```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper
DAGSTER_HOME=.dagster_home dagster dev
```

### 2. Open UI:
Go to http://localhost:3000

### 3. Explore Assets:
- **Assets** tab
- Click `jobs_with_skills`
- See: depends on job_postings + job_skills + skills
- Click **Lineage** for visual graph

### 4. Materialize:
- Click **Materialize**
- Dagster automatically joins the data
- Get enriched dataset!

### 5. Query Results:
```bash
python view_assets.py jobs_with_skills
python view_assets.py skill_demand_summary
python view_assets.py company_job_summary
```

## Key Takeaways

✅ **CSVs were always linked** - They use foreign keys  
✅ **Relationships are now explicit** - Shown in Dagster lineage  
✅ **Pre-joined data available** - No manual joining needed  
✅ **Aggregations ready to use** - Summary views pre-built  
✅ **Visually clear** - See dependencies in the UI  

## Next: What You Can Build

With linked assets, you can now easily:
- Find jobs by skill requirement
- See skill demand across companies
- Calculate average salary by industry
- Identify top companies by job count
- Create dashboards with interconnected data
- Feed data to ML models with proper relationships

All with Dagster handling the dependencies automatically! 🚀
