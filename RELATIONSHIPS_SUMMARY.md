# CSV Relationships - Summary

## Your Question
"Why doesn't the csvs link to each other? The job_id, company_id, skill, and salary_id should link to each other"

## The Answer
✅ **They DO link to each other!** - Using foreign keys just like a relational database.

## The Relationships

### Foreign Keys Found:
```
✓ job_postings.company_id     → companies.company_id
✓ job_postings.job_id         → benefits.job_id
✓ job_postings.job_id         → salaries.job_id
✓ job_postings.job_id         → job_skills.job_id
✓ job_postings.job_id         → job_industries.job_id
✓ job_skills.skill_abr        → skills.skill_abr
✓ job_industries.industry_id  → industries.industry_id
✓ companies.company_id        → company_industries.company_id
✓ companies.company_id        → company_specialities.company_id
✓ companies.company_id        → employee_counts.company_id
```

## What Was Added

### 1. **New Linked Assets** in `dagster_relationships.py`:

#### Direct Joins (Show relationships):
- `jobs_with_companies` - Join job_postings + companies
- `jobs_with_skills` - Join job_postings + job_skills + skills
- `jobs_with_industries` - Join job_postings + job_industries + industries
- `jobs_with_salaries` - Join job_postings + salaries
- `companies_with_industries` - Join companies + company_industries

#### Complete Views:
- `complete_job_data` - Everything: jobs + companies + skills + salaries

#### Aggregations (Summary views):
- `company_job_summary` - Jobs per company
- `skill_demand_summary` - Which skills are most demanded
- `industry_job_summary` - Jobs per industry

### 2. **Updated Files**:
- `scripts/definitions.py` - Now loads both `dagster_assets` and `dagster_relationships`
- `CSV_RELATIONSHIPS.md` - ER diagram showing all relationships
- `CSV_LINKS_EXPLAINED.md` - Detailed explanation with examples

## How to See the Relationships

### Visual Lineage in Dagster UI
1. Start Dagster: `cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && DAGSTER_HOME=.dagster_home dagster dev`
2. Go to **Assets** tab
3. Click on `jobs_with_companies`
4. You'll see it depends on:
   - `job_postings_csv`
   - `companies_csv`
5. Click "View Lineage" to see the graph

### Materialize and Inspect
1. Click **Materialize** on `jobs_with_companies`
2. Dagster automatically materializes dependencies first
3. Get a CSV with both job AND company data joined together

### Data Examples

**Before (Separate):**
```
job_postings.csv:
job_id | company_id | title
1      | 100        | Software Engineer
2      | 100        | Data Scientist

companies.csv:
company_id | name
100        | Google
200        | Apple
```

**After (jobs_with_companies):**
```
job_id | company_id | title           | name
1      | 100        | Software Eng    | Google
2      | 100        | Data Scientist  | Google
```

## Many-to-Many Example: Skills

**Three tables working together:**
```
job_postings:           job_skills:          skills:
job_id | title          job_id | skill_abr   skill_abr | skill_name
1      | Eng            1      | PYT         PYT       | Python
2      | Data Eng       1      | SQL         SQL       | SQL
                        1      | AWS         AWS       | AWS
                        2      | PYT
                        2      | SPAK
```

**Result (jobs_with_skills):**
```
job_id | title    | skill_abr | skill_name
1      | Eng      | PYT       | Python
1      | Eng      | SQL       | SQL
1      | Eng      | AWS       | AWS
2      | Data Eng | PYT       | Python
2      | Data Eng | SPAK      | Spark
```

## Next Steps

1. **Run Dagster**: `cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && DAGSTER_HOME=.dagster_home dagster dev`
2. **Materialize** `skill_demand_summary` to see top-demanded skills
3. **Explore** `complete_job_data` for full enriched data
4. **Create more ops** for your specific analysis needs

## Files to Read

- 📊 `CSV_RELATIONSHIPS.md` - Visual ER diagram
- 🔗 `CSV_LINKS_EXPLAINED.md` - Detailed explanation with examples
- 💻 `scripts/dagster_relationships.py` - The actual code

---

**Bottom Line:** Your CSVs were already linked with foreign keys. We've now made these relationships **visible and usable** in Dagster with pre-joined datasets and lineage graphs! 🎉
