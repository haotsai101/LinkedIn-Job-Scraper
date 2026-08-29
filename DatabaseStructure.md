# Database Structure

<!-- GENERATED FILE — do not edit by hand.
     Regenerate with:  python scripts/dump_schema.py > DatabaseStructure.md
     Schema DDL for fresh databases lives in scripts/create_db.py;
     migrations that evolve an existing database live in scripts/migrations/. -->

## benefits

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| job_id | INTEGER | yes |  | 1 |
| inferred | INTEGER | yes |  |  |
| type | TEXT | yes |  | 2 |

## blocked_entities

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| kind | TEXT | yes |  | 1 |
| pattern | TEXT | yes |  | 2 |
| reason | TEXT |  |  |  |

## companies

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| company_id | INTEGER |  |  | 1 |
| name | TEXT |  |  |  |
| description | TEXT |  |  |  |
| company_size | INTEGER |  |  |  |
| state | TEXT |  |  |  |
| country | TEXT |  |  |  |
| city | TEXT |  |  |  |
| zip_code | TEXT |  |  |  |
| address | TEXT |  |  |  |
| url | TEXT |  |  |  |

## company_industries

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| company_id | INTEGER | yes |  | 1 |
| industry | INTEGER | yes |  | 2 |

## company_specialities

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| company_id | INTEGER | yes |  | 1 |
| speciality | INTEGER | yes |  | 2 |

## employee_counts

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| company_id | INTEGER | yes |  | 2 |
| employee_count | INTEGER |  |  | 1 |
| follower_count | INTEGER |  |  |  |
| time_recorded | INTEGER | yes |  |  |

## industries

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| industry_id | INTEGER |  |  | 1 |
| industry_name | TEXT |  |  |  |

## job_industries

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| job_id | INTEGER |  |  | 1 |
| industry_id | INTEGER |  |  | 2 |

## job_skills

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| job_id | INTEGER |  |  | 1 |
| skill_abr | TEXT |  |  | 2 |

## jobs

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| job_id | INTEGER |  |  | 1 |
| scraped | INTEGER | yes | 0 |  |
| company_id | INTEGER |  |  |  |
| work_type | TEXT |  |  |  |
| formatted_work_type | TEXT |  |  |  |
| location | TEXT |  |  |  |
| job_posting_url | TEXT |  |  |  |
| applies | INTEGER |  |  |  |
| original_listed_time | TEXT |  |  |  |
| remote_allowed | INTEGER |  |  |  |
| application_url | TEXT |  |  |  |
| application_type | TEXT |  |  |  |
| expiry | TEXT |  |  |  |
| inferred_benefits | TEXT |  |  |  |
| closed_time | TEXT |  |  |  |
| formatted_experience_level | TEXT |  |  |  |
| years_experience | INTEGER |  |  |  |
| description | TEXT |  |  |  |
| title | TEXT |  |  |  |
| skills_desc | TEXT |  |  |  |
| views | INTEGER |  |  |  |
| job_region | TEXT |  |  |  |
| listed_time | TEXT |  |  |  |
| degree | TEXT |  |  |  |
| posting_domain | TEXT |  |  |  |
| sponsored | INTEGER |  |  |  |
| applied | INTEGER |  | NULL |  |
| listed_epoch | INTEGER |  |  |  |

Indexes:
- `idx_jobs_apptype` — `CREATE INDEX idx_jobs_apptype ON jobs(application_type)`
- `idx_jobs_company` — `CREATE INDEX idx_jobs_company ON jobs(company_id)`
- `idx_jobs_listed` — `CREATE INDEX idx_jobs_listed ON jobs(applied, listed_epoch DESC)`
- `idx_jobs_pending` — `CREATE INDEX idx_jobs_pending ON jobs(applied, scraped)`

## salaries

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| salary_id | INTEGER |  |  | 1 |
| job_id | INTEGER | yes |  |  |
| max_salary | FLOAT |  |  |  |
| med_salary | FLOAT |  |  |  |
| min_salary | FLOAT |  |  |  |
| pay_period | TEXT |  |  |  |
| currency | TEXT |  |  |  |
| compensation_type | TEXT |  |  |  |

## skills

| Column | Type | Not Null | Default | PK |
| --- | --- | --- | --- | --- |
| skill_abr | TEXT |  |  | 1 |
| skill_name | TEXT |  |  |  |
