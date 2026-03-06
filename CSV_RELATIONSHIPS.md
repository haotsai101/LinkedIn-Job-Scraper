# CSV Relationships and Data Model

## Entity Relationship Diagram

```
┌─────────────────────────┐
│   job_postings          │
│   ├── job_id (PK)      │◄─────┐
│   ├── company_id (FK)  │      │
│   ├── title            │      │
│   ├── description      │      │
│   └── ...              │      │
└─────────────────────────┘      │
         │                        │
         │ company_id (FK)       │ job_id (FK)
         │                        │
         ▼                        │
┌─────────────────────────┐      │
│   companies (PK)        │      │
│   ├── company_id        │      │
│   ├── name              │      │
│   ├── description       │      │
│   └── ...               │      │
└─────────────────────────┘      │
         │                        │
         ├─────────────────────┼──────────────┐
         │                    │              │
         │                    │              │
         ▼                    ▼              ▼
    ┌─────────────────┐  ┌──────────┐  ┌──────────────┐
    │ company_        │  │ employee │  │   benefits   │
    │ industries      │  │ _counts  │  │   (job_id)   │
    └─────────────────┘  └──────────┘  └──────────────┘
         │ industry_id
         │
         ▼
    ┌──────────────┐
    │ industries   │
    │ ├── industry_id (PK)
    │ └── industry_name
    └──────────────┘


    ┌─────────────────────────┐
    │   job_postings          │
    │   └── job_id (FK)       │◄─────┐
    └─────────────────────────┘      │
            │                        │
            ├─────────────────────┼──────────────┐
            │                    │              │
            ▼                    ▼              ▼
        ┌──────────────┐  ┌────────────┐  ┌─────────────┐
        │ job_skills   │  │ job_        │  │  salaries   │
        │ (skill_abr)  │  │ industries  │  │ (salary_id) │
        │              │  │(industry_id)│  │ (job_id)    │
        └──────────────┘  └────────────┘  └─────────────┘
            │                 │
            │ skill_abr       │ industry_id
            │                 │
            ▼                 ▼
        ┌──────────────┐  ┌──────────────┐
        │   skills     │  │ industries   │
        │ ├── skill_abr│  │ ├── industry_│
        │ └── skill_   │  │ │    id (PK) │
        │    name      │  │ └── industry_│
        └──────────────┘  │     name     │
                          └──────────────┘
```

## Foreign Key Relationships

| Table | Foreign Key | References | Primary Key |
|-------|-------------|-----------|------------|
| `job_postings` | `company_id` | `companies` | `company_id` |
| `job_postings` | `job_id` | Multiple tables | - |
| `benefits` | `job_id` | `job_postings` | `job_id` |
| `job_skills` | `job_id` | `job_postings` | `job_id` |
| `job_skills` | `skill_abr` | `skills` | `skill_abr` |
| `job_industries` | `job_id` | `job_postings` | `job_id` |
| `job_industries` | `industry_id` | `industries` | `industry_id` |
| `salaries` | `job_id` | `job_postings` | `job_id` |
| `company_industries` | `company_id` | `companies` | `company_id` |
| `company_industries` | `industry` | `industries` | `industry_name` or `industry_id` |
| `company_specialities` | `company_id` | `companies` | `company_id` |
| `employee_counts` | `company_id` | `companies` | `company_id` |

## Why They're Not Linked in Dagster Yet

Dagster assets are currently independent - they just load CSV files. To create **relationships** and **lineage** in Dagster, you need to:

1. **Create Dependency Assets** - Make assets depend on each other
2. **Merge/Join Data** - Create ops that join related tables
3. **Define Multi-Asset** - Create complex data pipelines that show relationships

## Next Steps

See `ENHANCE_DAGSTER_RELATIONSHIPS.md` for how to:
- Add asset dependencies
- Create linked data operations
- Build a proper data warehouse structure
