#!/bin/bash

# LinkedIn Job Scraper - Manual Job Execution Script

PROJECT_DIR="/Users/zhihao/personal_projects/LinkedIn-Job-Scraper"
cd "$PROJECT_DIR"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}LinkedIn Job Scraper - Dagster Job Runner${NC}\n"

# Function to create search config
create_search_config() {
    KEYWORDS=${1:-"software engineer"}
    PAGES=${2:-2}
    
    cat > /tmp/search_config.yaml << EOF
ops:
  search_jobs_op:
    config:
      keywords: "$KEYWORDS"
      pages_to_fetch: $PAGES
EOF
    echo -e "${GREEN}✓ Created search config: keywords='$KEYWORDS', pages=$PAGES${NC}"
}

# Function to create details config
create_details_config() {
    MAX_UPDATES=${1:-50}
    SLEEP_TIME=${2:-30}
    
    cat > /tmp/details_config.yaml << EOF
ops:
  fetch_job_details_op:
    config:
      max_updates: $MAX_UPDATES
      sleep_time: $SLEEP_TIME
EOF
    echo -e "${GREEN}✓ Created details config: max_updates=$MAX_UPDATES, sleep_time=$SLEEP_TIME${NC}"
}

create_both_config() {
    KEYWORDS=${1:-"software engineer"}
    PAGES=${2:-2}
    MAX_UPDATES=${4:-25}
    SLEEP_TIME=${3:-30}

    cat > /tmp/both_config.yaml << EOF
ops:
  search_jobs_op:
    config:
      keywords: "$KEYWORDS"
      pages_to_fetch: $PAGES
  fetch_job_details_op:
    config:
      max_updates: $MAX_UPDATES
      sleep_time: $SLEEP_TIME
EOF
    echo -e "${GREEN}✓ Created combined config: keywords='$KEYWORDS', pages=$PAGES${NC}, max_updates=$MAX_UPDATES, sleep_time=$SLEEP_TIME${NC}"
}


# Main menu
case "${1:-menu}" in
    search)
        echo -e "${BLUE}Running: Search Jobs${NC}"
        create_search_config "${2:-software engineer}" "${3:-2}"
        echo -e "${BLUE}Executing...${NC}\n"
        dagster job execute -m scripts.definitions -j search_jobs_only -c /tmp/search_config.yaml
        ;;
    details)
        echo -e "${BLUE}Running: Fetch Job Details${NC}"
        create_details_config "${2:-50}" "${3:-30}"
        echo -e "${BLUE}Executing...${NC}\n"
        dagster job execute -m scripts.definitions -j fetch_details_only -c /tmp/details_config.yaml
        ;;
    both)
        echo -e "${BLUE}Running: Search + Fetch Details${NC}"
        create_both_config "${2:-software engineer}" "${3:-2}" "${4:-25}" "${5:-30}"
        echo -e "${BLUE}Executing...${NC}\n"
        dagster job execute -m scripts.definitions -j search_and_fetch_jobs -c /tmp/both_config.yaml
        ;;
    *)
        echo "Usage: ./run_jobs.sh <command> [args]"
        echo ""
        echo "Commands:"
        echo "  search [keywords] [pages]                             Search for jobs (default: 'software engineer', 2 pages)"
        echo "  details [max_updates] [sleep]                         Fetch job details (default: 50 jobs, 30s sleep)"
        echo "  both [keywords] [pages] [max_updates] [sleep]         Search + fetch details"
        echo ""
        echo "Examples:"
        echo "  ./run_jobs.sh search"
        echo "  ./run_jobs.sh search 'data scientist' 5"
        echo "  ./run_jobs.sh details 100 30"
        echo "  ./run_jobs.sh both 'machine learning' 3 50 20"
        ;;
esac
