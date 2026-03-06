#!/usr/bin/env python
"""
Simple script to view CSV asset data and Dagster materialized assets.
This script loads and displays both CSV files and Dagster-materialized data.
"""

import pandas as pd
import pickle
from pathlib import Path

# Get the project root
PROJECT_ROOT = Path(__file__).parent
CSV_FOLDER = PROJECT_ROOT / "csv_files"
DAGSTER_HOME = PROJECT_ROOT / ".dagster_home"
STORAGE_FOLDER = DAGSTER_HOME / "storage"

def load_dagster_asset(asset_name: str) -> pd.DataFrame:
    """Load a Dagster materialized asset from storage."""
    asset_path = STORAGE_FOLDER / asset_name
    
    if not asset_path.exists():
        return None
    
    # Try to load the pickle file
    try:
        with open(asset_path, 'rb') as f:
            return pickle.load(f)
    except Exception as e:
        print(f"Error loading Dagster asset: {e}")
        return None

def display_asset(filename: str, max_rows: int = 10):
    """Display a CSV asset or Dagster materialized asset with its schema and sample data."""
    
    # First try as Dagster asset
    df = load_dagster_asset(filename)
    if df is not None:
        asset_type = "🗄️  Dagster Materialized"
    else:
        # Try as CSV file
        filepath = CSV_FOLDER / f"{filename}.csv"
        
        if not filepath.exists():
            print(f"\n❌ Asset not found: {filename}")
            print(f"   Not in: {filepath}")
            print(f"   Not in: {STORAGE_FOLDER / filename}")
            print(f"\n💡 Tip: Materialized assets must be loaded via Dagster first!")
            print(f"   Run: DAGSTER_HOME=.dagster_home dagster dev")
            print(f"   Then materialize the assets in the UI")
            return
        
        df = pd.read_csv(filepath)
        asset_type = "📄 CSV File"
    
    print(f"\n{'='*80}")
    print(f"Asset: {filename} ({asset_type})")
    print(f"{'='*80}")
    print(f"\n📊 Shape: {df.shape[0]} rows × {df.shape[1]} columns")
    print(f"\n📋 Schema:")
    print(df.dtypes)
    print(f"\n📈 Summary Statistics:")
    print(df.describe())
    print(f"\n📝 First {max_rows} rows:")
    print(df.head(max_rows).to_string())
    print()

def list_all_assets():
    """List all available assets (both CSV and Dagster materialized)."""
    print("\n🗂️  Available CSV Assets:")
    print("="*80)
    csv_files = sorted([f.stem for f in CSV_FOLDER.glob("*.csv")])
    for i, name in enumerate(csv_files, 1):
        filepath = CSV_FOLDER / f"{name}.csv"
        df = pd.read_csv(filepath)
        size = filepath.stat().st_size / 1024
        print(f"{i:2d}. {name:40s} | Rows: {len(df):6d} | Size: {size:8.1f}KB | 📄 CSV")
    
    # List Dagster materialized assets
    print("\n�️  Available Dagster Materialized Assets:")
    print("="*80)
    if STORAGE_FOLDER.exists():
        dagster_assets = sorted([f.name for f in STORAGE_FOLDER.iterdir() if f.is_file()])
        if dagster_assets:
            for i, name in enumerate(dagster_assets, 1):
                try:
                    df = load_dagster_asset(name)
                    if df is not None:
                        size = STORAGE_FOLDER.joinpath(name).stat().st_size / 1024
                        print(f"{i:2d}. {name:40s} | Rows: {len(df):6d} | Size: {size:8.1f}KB | 🗄️  Materialized")
                except:
                    pass
        else:
            print("   (No materialized assets yet. Materialize in Dagster UI first!)")
    else:
        print("   (Dagster storage not found. Run Dagster and materialize assets.)")
    print()

def main():
    """Main function."""
    import sys
    
    if len(sys.argv) < 2:
        print("\n🔍 Usage:")
        print("  python view_assets.py list              # List all assets")
        print("  python view_assets.py <asset_name>      # View specific asset")
        print("  python view_assets.py all               # View all assets")
        print("\n📚 CSV Examples:")
        print("  python view_assets.py job_postings")
        print("  python view_assets.py companies")
        print("  python view_assets.py skills")
        print("\n🗄️  Materialized Asset Examples:")
        print("  python view_assets.py jobs_with_companies")
        print("  python view_assets.py skill_demand_summary")
        print("  python view_assets.py complete_job_data")
        print("\n💡 Note: Materialized assets must be loaded in Dagster UI first!")
        print("  1. Run: DAGSTER_HOME=.dagster_home dagster dev")
        print("  2. Go to Assets tab")
        print("  3. Click Materialize on desired assets")
        print()
        list_all_assets()
        return
    
    command = sys.argv[1]
    
    if command == "list":
        list_all_assets()
    elif command == "all":
        csv_files = sorted([f.stem for f in CSV_FOLDER.glob("*.csv")])
        for name in csv_files:
            display_asset(name, max_rows=5)
    else:
        display_asset(command)

if __name__ == "__main__":
    main()
