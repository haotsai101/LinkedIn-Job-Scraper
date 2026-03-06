"""
Enhanced Dagster assets with relationships for LinkedIn Job Scraper.
This module includes assets that show data relationships and create linked datasets.
These depend on the database assets defined in dagster_db_assets.py.
"""

import pandas as pd
from pathlib import Path

from dagster import asset, get_dagster_logger

logger = get_dagster_logger()


# ============================================================================
# LINKED/ENRICHED ASSETS - Show relationships
# These assets depend on the database assets from dagster_db_assets.py
# ============================================================================

@asset
def jobs_with_companies(
    jobs_db: pd.DataFrame,
    companies_db: pd.DataFrame
) -> pd.DataFrame:
    """
    Join jobs with companies.
    Shows the relationship: jobs.company_id -> companies.company_id
    """
    logger.info("Joining jobs with companies...")
    result = jobs_db.merge(
        companies_db,
        on='company_id',
        how='left',
        suffixes=('_job', '_company')
    )
    logger.info(f"Created {len(result)} job-company records")
    return result


@asset
def jobs_with_skills(
    jobs_db: pd.DataFrame,
    job_skills_db: pd.DataFrame,
    skills_db: pd.DataFrame
) -> pd.DataFrame:
    """
    Join jobs -> job_skills -> skills.
    Shows relationships:
    - jobs.job_id -> job_skills.job_id
    - job_skills.skill_abr -> skills.skill_abr
    """
    logger.info("Joining jobs with skills...")
    
    # First join jobs with job_skills
    jobs_skills = jobs_db[['job_id', 'title', 'company_id']].merge(
        job_skills_db,
        on='job_id',
        how='left'
    )
    
    # Then join with skills master data
    result = jobs_skills.merge(
        skills_db,
        on='skill_abr',
        how='left'
    )
    
    logger.info(f"Created {len(result)} job-skill relationships")
    return result


@asset
def jobs_with_industries(
    jobs_db: pd.DataFrame,
    job_industries_db: pd.DataFrame,
    industries_db: pd.DataFrame
) -> pd.DataFrame:
    """
    Join jobs -> job_industries -> industries.
    Shows relationships:
    - jobs.job_id -> job_industries.job_id
    - job_industries.industry_id -> industries.industry_id
    """
    logger.info("Joining jobs with industries...")
    
    # First join jobs with job_industries
    jobs_industries = jobs_db[['job_id', 'title', 'company_id']].merge(
        job_industries_db,
        on='job_id',
        how='left'
    )
    
    # Then join with industries master data
    result = jobs_industries.merge(
        industries_db,
        on='industry_id',
        how='left'
    )
    
    logger.info(f"Created {len(result)} job-industry relationships")
    return result


@asset
def jobs_with_salaries(
    jobs_db: pd.DataFrame,
    salaries_db: pd.DataFrame
) -> pd.DataFrame:
    """
    Join jobs with salaries.
    Shows relationship: jobs.job_id -> salaries.job_id
    """
    logger.info("Joining jobs with salaries...")
    result = jobs_db[['job_id', 'title', 'company_id']].merge(
        salaries_db,
        on='job_id',
        how='left'
    )
    logger.info(f"Created {len(result)} job-salary records")
    return result


@asset
def companies_with_industries(
    companies_db: pd.DataFrame,
    company_industries_db: pd.DataFrame
) -> pd.DataFrame:
    """
    Join companies with industries.
    Shows relationship: companies.company_id -> company_industries.company_id
    """
    logger.info("Joining companies with industries...")
    result = companies_db[['company_id', 'name']].merge(
        company_industries_db,
        on='company_id',
        how='left'
    )
    logger.info(f"Created {len(result)} company-industry records")
    return result


@asset
def complete_job_data(
    jobs_with_companies: pd.DataFrame,
    job_skills_db: pd.DataFrame,
    salaries_db: pd.DataFrame
) -> pd.DataFrame:
    """
    Complete view combining jobs, companies, and multiple relations.
    Demonstrates multiple relationship layers.
    """
    logger.info("Creating complete job dataset...")
    
    # Start with jobs + companies
    result = jobs_with_companies.copy()
    
    # Add count of skills per job
    skills_per_job = job_skills_db.groupby('job_id').size().reset_index(name='skill_count')
    result = result.merge(skills_per_job, on='job_id', how='left')
    
    # Add salary info
    result = result.merge(
        salaries_db[['job_id', 'min_salary', 'med_salary', 'max_salary']],
        on='job_id',
        how='left'
    )
    
    logger.info(f"Created complete view with {len(result)} jobs")
    return result


# ============================================================================
# AGGREGATION ASSETS - Summary views
# ============================================================================

@asset
def company_job_summary(
    jobs_with_companies: pd.DataFrame
) -> pd.DataFrame:
    """Summary: Jobs count per company."""
    logger.info("Creating company job summary...")
    summary = jobs_with_companies.groupby(['company_id', 'name']).agg({
        'job_id': 'count',
        'title': lambda x: ', '.join(x.head(3))
    }).reset_index()
    summary.columns = ['company_id', 'company_name', 'job_count', 'sample_titles']
    logger.info(f"Created summary for {len(summary)} companies")
    return summary


@asset
def skill_demand_summary(
    jobs_with_skills: pd.DataFrame
) -> pd.DataFrame:
    """Summary: Top skills by job demand."""
    logger.info("Creating skill demand summary...")
    summary = jobs_with_skills.groupby(['skill_abr', 'skill_name']).agg({
        'job_id': 'count'
    }).reset_index()
    summary.columns = ['skill_abr', 'skill_name', 'job_count']
    summary = summary.sort_values('job_count', ascending=False)
    logger.info(f"Top skills: {summary.head(5).to_dict('records')}")
    return summary


@asset
def industry_job_summary(
    jobs_with_industries: pd.DataFrame
) -> pd.DataFrame:
    """Summary: Jobs by industry."""
    logger.info("Creating industry job summary...")
    summary = jobs_with_industries.groupby(['industry_id', 'industry_name']).agg({
        'job_id': 'count'
    }).reset_index()
    summary.columns = ['industry_id', 'industry_name', 'job_count']
    summary = summary.sort_values('job_count', ascending=False)
    logger.info(f"Top industries: {summary.head(5).to_dict('records')}")
    return summary
