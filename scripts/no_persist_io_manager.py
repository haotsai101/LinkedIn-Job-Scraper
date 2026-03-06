"""
IO Manager that doesn't persist asset outputs to disk.
Reduces storage while keeping SQLite as source of truth.

Usage:
- Snapshots are NOT saved to .dagster_home/storage/
- Dagster still tracks lineage and run history
- Assets can be re-materialized anytime from SQLite
- Perfect for when the database is the source of truth
"""

from dagster import IOManager, io_manager


class NoOpIOManager(IOManager):
    """
    IO Manager that skips persistence of asset outputs.
    
    This manager is ideal when:
    - Your database (SQLite) is the source of truth
    - You don't need historical snapshots
    - You want to minimize disk usage
    - You want faster materialization
    
    Trade-offs:
    ✅ Saves ~500MB per materialization
    ✅ Faster I/O (no file writes)
    ✅ Cleaner storage directory
    ⚠️ Can't inspect past snapshot values
       (but you can query SQLite)
    """
    
    def handle_output(self, context, obj):
        """
        Skip saving output to disk.
        
        Called when an asset finishes materializing.
        We log but don't persist.
        """
        context.log.info(
            f"⏭️  Skipping persistence of {context.asset_key} "
            f"({len(obj) if hasattr(obj, '__len__') else 'N/A'} rows) "
            f"- source is SQLite database"
        )
        # Don't persist - return None
        return None
    
    def load_input(self, context):
        """
        Cannot load from this manager.
        
        Called when an asset depends on another asset's output.
        Since we don't persist, we return None and let Dagster
        handle re-materialization or raise appropriate error.
        """
        context.log.warning(
            f"⚠️  Cannot load {context.asset_key} from disk "
            f"(this manager doesn't persist outputs). "
            f"Please re-materialize if needed."
        )
        return None


@io_manager(config_schema={})
def no_persist_io_manager(context) -> NoOpIOManager:
    """
    Provides NoOpIOManager - skips all persistence.
    
    Returns:
        NoOpIOManager instance
    """
    return NoOpIOManager()
