#!/bin/bash
# Dagster setup script for LinkedIn Job Scraper

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DAGSTER_HOME="$PROJECT_DIR/.dagster_home"

echo "Setting up Dagster for LinkedIn Job Scraper..."
echo "Project directory: $PROJECT_DIR"
echo "Dagster home: $DAGSTER_HOME"

# Create .dagster_home directory if it doesn't exist
mkdir -p "$DAGSTER_HOME"

# Create dagster.yaml in .dagster_home
cat > "$DAGSTER_HOME/dagster.yaml" << EOF
# Dagster instance configuration for LinkedIn Job Scraper
# This file configures storage and runtime settings

storage:
  sqlite:
    base_dir: $DAGSTER_HOME

code_servers:
  local_startup_timeout: 30
EOF

echo "✓ Created $DAGSTER_HOME/dagster.yaml"

# Test if dependencies are installed
python -c "import dagster; print(f'✓ Dagster {dagster.__version__} is installed')" 2>/dev/null || {
    echo "✗ Dagster is not installed. Run: pip install -r requirements.txt"
    exit 1
}

# Test if definitions can be imported
cd "$PROJECT_DIR"
python -c "from scripts.definitions import defs; print('✓ Dagster definitions loaded successfully')" || {
    echo "✗ Failed to load Dagster definitions"
    exit 1
}

echo ""
echo "Setup complete! ✓"
echo ""
echo "To start Dagster, run:"
echo "  export DAGSTER_HOME=$DAGSTER_HOME"
echo "  cd $PROJECT_DIR"
echo "  dagster dev"
echo ""
echo "Or use the one-liner:"
echo "  DAGSTER_HOME=$DAGSTER_HOME dagster dev -d $PROJECT_DIR"
echo ""
echo "⚠️  IMPORTANT: Always run 'dagster dev' from the PROJECT ROOT:"
echo "  cd $PROJECT_DIR"
