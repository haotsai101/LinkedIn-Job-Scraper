#!/bin/bash
# Quick launcher script for Dagster
# Usage: ./run_dagster.sh

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DAGSTER_HOME="$PROJECT_DIR/.dagster_home"

echo "🚀 Starting Dagster for LinkedIn Job Scraper"
echo "📁 Project directory: $PROJECT_DIR"
echo "💾 Dagster home: $DAGSTER_HOME"
echo ""

# Verify we're in the right directory
if [ ! -f "$PROJECT_DIR/scripts/definitions.py" ]; then
    echo "❌ Error: Could not find scripts/definitions.py"
    echo "   Make sure you're running this script from the project root"
    exit 1
fi

# Verify DAGSTER_HOME exists
if [ ! -d "$DAGSTER_HOME" ]; then
    echo "📁 Creating $DAGSTER_HOME..."
    mkdir -p "$DAGSTER_HOME"
fi

# Check if dagster.yaml exists in DAGSTER_HOME
if [ ! -f "$DAGSTER_HOME/dagster.yaml" ]; then
    echo "⚙️  Creating Dagster configuration..."
    cat > "$DAGSTER_HOME/dagster.yaml" << EOF
# Dagster instance configuration for LinkedIn Job Scraper
storage:
  sqlite:
    base_dir: $DAGSTER_HOME
code_servers:
  local_startup_timeout: 30
EOF
fi

echo ""
echo "✅ Configuration verified"
echo ""
echo "🌐 Dagster will be available at: http://localhost:3000"
echo ""
echo "📝 To materialize assets:"
echo "   1. Open http://localhost:3000 in your browser"
echo "   2. Go to the 'Assets' tab"
echo "   3. Click 'Materialize All'"
echo ""

# Start Dagster
export DAGSTER_HOME=$DAGSTER_HOME
cd "$PROJECT_DIR"
dagster dev
