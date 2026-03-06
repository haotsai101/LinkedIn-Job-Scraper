# Fix for Dagster Import Error

## Problem
You got this error when starting Dagster:
```
ModuleNotFoundError: No module named 'scripts'
while importing module scripts.definitions
```

## Root Cause
Dagster was trying to run from the `.dagster_home` directory, which doesn't contain the `scripts` module. The `scripts` module is in the project root.

## Solution

### ✅ The Correct Way to Start Dagster

**Always run from the PROJECT ROOT directory:**

```bash
# Step 1: Navigate to project root
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper

# Step 2: Set DAGSTER_HOME and start Dagster
DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home dagster dev
```

**Or use the one-liner:**
```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper && \
DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home dagster dev
```

### ⚠️ What NOT to Do

❌ **Don't run from inside `.dagster_home`:**
```bash
cd /Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home
dagster dev  # This will fail!
```

## Key Points

1. **Working Directory**: Dagster must run from `/Users/zhihao/personal_projects/LinkedIn-Job-Scraper` (project root)
2. **DAGSTER_HOME**: Points to `.dagster_home` for storing instance data
3. **Module Path**: Python needs to find `scripts/definitions.py` relative to the working directory

## Updated Files

- `setup_dagster.sh` - Updated with correct instructions
- `.dagster_home/dagster.yaml` - Updated configuration

## Quick Reference

```bash
# Set these in your shell profile (~/.zshrc or ~/.bash_profile)
export DAGSTER_HOME=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper/.dagster_home
export PROJECT_ROOT=/Users/zhihao/personal_projects/LinkedIn-Job-Scraper

# Then you can simply:
cd $PROJECT_ROOT
dagster dev
```

## Troubleshooting

If you still get the error:
1. Make sure you're in the project root: `pwd` should show `.../LinkedIn-Job-Scraper`
2. Make sure `scripts/definitions.py` exists: `ls scripts/definitions.py`
3. Make sure `DAGSTER_HOME` is set: `echo $DAGSTER_HOME`
4. Try with verbose logging: `dagster dev --verbose` (from project root)
