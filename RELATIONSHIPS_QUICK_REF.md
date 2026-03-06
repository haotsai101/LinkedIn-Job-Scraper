# Quick Reference: CSV Relationships

## 🔗 The Linking Pattern

All your CSVs are linked through **IDs**:

```
JOBS are the CENTER:
        job_postings
            ↙↓↘
           ↙ ↓ ↘
          /  ↓   \
    company skill  industry  salary  benefit
        (via company_id)  (via job_id everywhere else)
```

## 📋 What Links to What

### Core Links (PRIMARY):
| From | To | Via | Type |
|------|----|----|------|
| job_postings | companies | company_id | FK |
| job_postings | salaries | job_id | FK |
| job_postings | benefits | job_id | FK |

### Many-to-Many Links (JUNCTION TABLES):
| From | Junction | To | Type |
|------|----------|----|----|
| job_postings | job_skills | skills | M2M |
| job_postings | job_industries | industries | M2M |
| companies | company_industries | industries | M2M |

### One-to-Many Links:
| From | To | Via | Type |
|------|----|----|------|
| companies | company_specialities | company_id | FK |
| companies | employee_counts | company_id | FK |

## 🎯 New Pre-Joined Assets

✅ Already created for you! These show the relationships:

**Single Joins (simple relationships):**
- `jobs_with_companies` - See job details with company info
- `jobs_with_salaries` - See job details with salary ranges
- `companies_with_industries` - See companies with their industries

**Multi-Joins (multiple relationships):**
- `jobs_with_skills` - See jobs with ALL required skills
- `jobs_with_industries` - See jobs with ALL industries
- `complete_job_data` - Jobs + Companies + Skills + Salaries (everything!)

**Aggregations (summaries):**
- `company_job_summary` - How many jobs per company?
- `skill_demand_summary` - Which skills are needed most?
- `industry_job_summary` - How many jobs per industry?

## 🚀 How to Use

### See the Relationship Visually
1. Open Dagster: `DAGSTER_HOME=.dagster_home dagster dev`
2. Click **Assets** → Find `jobs_with_companies`
3. Look for **"Lineage"** button → Shows dependency graph
4. See: `jobs_with_companies` depends on `job_postings_csv` + `companies_csv`

### Get Linked Data
1. Click **Materialize** on any linked asset (e.g., `jobs_with_skills`)
2. Dagster joins the data automatically
3. You get one DataFrame with all the linked data!

### Run Analysis
```bash
python view_assets.py jobs_with_companies  # View joined data
python view_assets.py skill_demand_summary # See top skills
python view_assets.py company_job_summary  # See jobs per company
```

## 📊 Example Data Flows

### Before (Separate Files):
```
job_postings.csv
├─ job_id: 1, company_id: 100, title: "Engineer"

companies.csv
├─ company_id: 100, name: "Google"
```

### After (jobs_with_companies):
```
job_id | company_id | title      | name
1      | 100        | Engineer   | Google
```

### Complex Example (jobs_with_skills):
```
Raw Data (3 tables):
- job_postings: job_id=1, title="Engineer"
- job_skills: (1, Python), (1, SQL), (1, AWS)
- skills: Python→PYT, SQL→SQL, AWS→AWS

Joined Result:
job_id | title    | skill | skill_name
1      | Engineer | PYT   | Python
1      | Engineer | SQL   | SQL  
1      | Engineer | AWS   | AWS
```

## 💾 Where Everything Is

**Original CSVs:**
```
csv_files/
├── job_postings.csv (main table)
├── companies.csv (joined by company_id)
├── job_skills.csv (junction: job → skills)
├── job_industries.csv (junction: job → industries)
├── skills.csv (skill master)
├── industries.csv (industry master)
├── salaries.csv (linked by job_id)
├── benefits.csv (linked by job_id)
├── company_industries.csv (company → industries)
├── company_specialities.csv (linked by company_id)
└── employee_counts.csv (linked by company_id)
```

**New Assets (Code):**
```
scripts/
├── dagster_assets.py (original: load raw CSVs)
└── dagster_relationships.py (NEW: create linked views!)
```

**Documentation:**
```
├── CSV_RELATIONSHIPS.md (ER diagram)
├── CSV_LINKS_EXPLAINED.md (detailed guide)
└── RELATIONSHIPS_SUMMARY.md (this file's big brother)
```

## ✨ The Bottom Line

Your data was **already linked** at the CSV level using foreign keys.  
We've now made these links **visible and usable** in Dagster! 🎉

When you materialize `jobs_with_skills`, Dagster automatically:
1. Loads `job_postings_csv`
2. Loads `job_skills_csv`
3. Loads `skills_csv`
4. Joins them all together
5. Gives you one clean DataFrame with everything linked!

No more manually joining CSVs in pandas! 🚀
