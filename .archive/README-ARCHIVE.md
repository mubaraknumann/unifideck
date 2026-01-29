# Deprecated Files Archive

This folder contains deprecated/superseded implementations that have been migrated to the modular backend architecture.

## Migration Status: COMPLETE ✅

**Migration Date**: 2026-01-29

### Archived Files

#### 1. `download_manager.py`
- **Status**: DEPRECATED - Replaced by modular backend
- **Size**: 46 KB
- **Reason**: Both root and defaults versions consolidated into single backend implementation
- **Migration**: main.py now imports from `backend.download.manager`

**Note**: This archive contains the consolidated deprecated download manager. The modular backend at `defaults/backend/download/manager.py` is the new single source of truth.

### New Single Source of Truth

**Location**: `defaults/backend/download/manager.py`

**Features**:
- ✅ Auto-cleanup of orphaned processes in `__init__`
- ✅ Auto-resume of queued downloads in `_load()`
- ✅ Fully self-contained initialization logic
- ✅ Modular backend architecture

### Migration Details

**main.py Changes**:
- Line 34: `from backend.download.manager import get_download_queue, DownloadQueue`
- Line 2857: `from backend.download.manager import DownloadStatus`
- Lines 2730-2734: Removed manual cleanup/start calls (now automatic)

### Safe to Delete

These archived files can be safely deleted once you're confident the migration is stable:

```bash
rm .archive/download_manager.py
rm .archive/defaults/download_manager.py
```

Or keep them for historical reference if needed.

### Rollback Instructions (If Needed)

If issues occur with the backend version:

```bash
# Restore from archive
cp .archive/download_manager.py .
cp .archive/defaults/download_manager.py defaults/

# Revert main.py
git checkout main.py
# or restore from backup
cp main.py.backup main.py
```

---

**Backend Migration**: Part of commit 33fbb79 (Jan 14) - ongoing modularization effort to consolidate on clean, maintainable backend architecture.
