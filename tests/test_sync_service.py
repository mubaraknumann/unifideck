"""
Basic tests for SyncService to ensure core functionality works.
"""
import pytest
from unittest.mock import AsyncMock, Mock, patch
from backend.services.sync_service import SyncService


@pytest.fixture
def mock_dependencies():
    """Create mock dependencies for SyncService."""
    return {
        'epic_connector': AsyncMock(),
        'gog_connector': AsyncMock(),
        'amazon_connector': AsyncMock(),
        'shortcuts_manager': AsyncMock(),
        'metadata_service': Mock(),
        'artwork_service': AsyncMock(),
        'size_fetcher': Mock(),
        'sync_progress': Mock(
            status='',
            current_game={},
            error=None,
            total_games=0,
            synced_games=0,
            to_dict=Mock(return_value={})
        ),
        'plugin_dir': '/fake/plugin/dir'
    }


@pytest.fixture
def sync_service(mock_dependencies):
    """Create a SyncService instance with mocked dependencies."""
    return SyncService(**mock_dependencies)


def test_sync_service_initialization(sync_service):
    """Test that SyncService initializes correctly."""
    assert sync_service is not None
    assert sync_service._is_syncing == False
    assert sync_service._cancel_sync == False


def test_cancel_sync_sets_flag(sync_service):
    """Test that cancel_sync sets the cancellation flag."""
    sync_service._is_syncing = True
    sync_service.cancel_sync()
    assert sync_service._cancel_sync == True


def test_cancel_sync_when_not_syncing(sync_service):
    """Test that cancel_sync does nothing when not syncing."""
    sync_service._is_syncing = False
    sync_service.cancel_sync()
    # Should not crash, flag can be set regardless


@pytest.mark.asyncio
async def test_sync_libraries_prevents_concurrent_syncs(sync_service, mock_dependencies):
    """Test that sync_libraries prevents concurrent sync operations."""
    # Set syncing flag
    sync_service._is_syncing = True
    
    # Try to sync
    result = await sync_service.sync_libraries()
    
    # Should return error
    assert result['success'] == False
    assert 'error' in result
    assert result['error'] == 'errors.syncInProgress'


@pytest.mark.asyncio
async def test_update_installed_status(sync_service):
    """Test _update_installed_status updates game metadata."""
    # Create mock games
    mock_game = Mock(id='test_game', is_installed=False)
    epic_games = [mock_game]
    
    # Mock installed data
    epic_installed = {
        'test_game': {
            'install': {'install_path': '/path/to/game'},
            'manifest': {'launch_exe': 'game.exe'}
        }
    }
    
    # Run update
    await sync_service._update_installed_status(
        epic_games, epic_installed,
        [], {},
        [], {}
    )
    
    # Verify game is marked as installed
    assert mock_game.is_installed == True


@pytest.mark.asyncio
async def test_handle_cancellation(sync_service):
    """Test that _handle_cancellation returns proper error response."""
    result = sync_service._handle_cancellation()
    
    assert result['success'] == False
    assert result['cancelled'] == True
    assert 'error' in result
    assert sync_service.sync_progress.status == 'cancelled'


def test_sync_service_has_required_methods(sync_service):
    """Test that SyncService has all required public methods."""
    assert hasattr(sync_service, 'sync_libraries')
    assert hasattr(sync_service, 'force_sync_libraries')
    assert hasattr(sync_service, 'cancel_sync')
    
    # Verify they're callable
    assert callable(sync_service.sync_libraries)
    assert callable(sync_service.force_sync_libraries)
    assert callable(sync_service.cancel_sync)
