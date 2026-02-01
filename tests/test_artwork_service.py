"""
Basic tests for ArtworkService to ensure core functionality works.
"""
import pytest
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
from backend.services.artwork_service import ArtworkService


@pytest.fixture
def mock_steamgriddb():
    """Create mock SteamGridDB client."""
    return Mock(
        grid_path='/fake/grid/path',
        fetch_game_art=AsyncMock(return_value={
            'success': True,
            'artwork_count': 4,
            'steam_app_id': 123456
        }),
        search_game=AsyncMock(return_value=123456)
    )


@pytest.fixture
def mock_sync_progress():
    """Create mock sync progress tracker."""
    return Mock(
        artwork_synced=0,
        artwork_total=0,
        current_game={},
        increment_artwork=AsyncMock(return_value=1)
    )


@pytest.fixture
def artwork_service(mock_steamgriddb, mock_sync_progress):
    """Create an ArtworkService instance with mocked dependencies."""
    return ArtworkService(
        steamgriddb_client=mock_steamgriddb,
        sync_progress=mock_sync_progress
    )


def test_artwork_service_initialization(artwork_service):
    """Test that ArtworkService initializes correctly."""
    assert artwork_service is not None
    assert artwork_service.steamgriddb is not None
    assert artwork_service.sync_progress is not None


@pytest.mark.asyncio
async def test_has_artwork_no_client(mock_sync_progress):
    """Test has_artwork returns False when no SteamGridDB client."""
    service = ArtworkService(None, mock_sync_progress)
    result = await service.has_artwork(12345)
    assert result == False


@pytest.mark.asyncio
async def test_get_missing_types_no_client(mock_sync_progress):
    """Test get_missing_types returns all types when no client."""
    service = ArtworkService(None, mock_sync_progress)
    result = await service.get_missing_types(12345)
    assert result == {'grid', 'hero', 'logo', 'icon'}


@pytest.mark.asyncio
async def test_search_game_returns_id(artwork_service, mock_steamgriddb):
    """Test search_game returns game ID from SteamGridDB."""
    result = await artwork_service.search_game("Test Game")
    assert result == 123456
    mock_steamgriddb.search_game.assert_called_once_with("Test Game")


@pytest.mark.asyncio
async def test_search_game_handles_exception(artwork_service, mock_steamgriddb):
    """Test search_game handles exceptions gracefully."""
    mock_steamgriddb.search_game.side_effect = Exception("API Error")
    result = await artwork_service.search_game("Test Game")
    assert result is None


def test_get_grid_path_returns_path(artwork_service):
    """Test get_grid_path returns the correct path."""
    result = artwork_service.get_grid_path()
    assert result == '/fake/grid/path'


def test_get_grid_path_no_client(mock_sync_progress):
    """Test get_grid_path returns None when no client."""
    service = ArtworkService(None, mock_sync_progress)
    result = service.get_grid_path()
    assert result is None


@pytest.mark.asyncio
async def test_fetch_for_game_success(artwork_service, mock_steamgriddb):
    """Test fetch_for_game successfully fetches artwork."""
    mock_game = Mock(
        title="Test Game",
        app_id=12345,
        store="epic",
        id="test_game_id"
    )
    
    result = await artwork_service.fetch_for_game(mock_game)
    
    assert result['success'] == True
    assert result['game'] == mock_game
    assert result['artwork_count'] == 4
    assert mock_game.steam_app_id == 123456


@pytest.mark.asyncio
async def test_fetch_for_game_timeout(artwork_service, mock_steamgriddb):
    """Test fetch_for_game handles timeout."""
    import asyncio
    mock_steamgriddb.fetch_game_art = AsyncMock(
        side_effect=asyncio.TimeoutError()
    )
    
    mock_game = Mock(
        title="Test Game",
        app_id=12345,
        store="epic",
        id="test_game_id"
    )
    
    result = await artwork_service.fetch_for_game(mock_game)
    
    assert result['success'] == False
    assert result['timed_out'] == True


@pytest.mark.asyncio
async def test_delete_game_artwork_no_client(mock_sync_progress):
    """Test delete_game_artwork returns False when no client."""
    service = ArtworkService(None, mock_sync_progress)
    result = await service.delete_game_artwork(12345)
    
    assert result['grid'] == False
    assert result['hero'] == False
    assert result['logo'] == False
    assert result['icon'] == False


def test_artwork_service_has_required_methods(artwork_service):
    """Test that ArtworkService has all required public methods."""
    required_methods = [
        'has_artwork',
        'get_missing_types',
        'get_missing_artwork_types',
        'fetch_for_game',
        'fetch_artwork_with_progress',
        'delete_game_artwork',
        'get_artwork_paths',
        'search_game',
        'cleanup_orphaned',
        'get_grid_path',
        'clear_cache'
    ]
    
    for method_name in required_methods:
        assert hasattr(artwork_service, method_name)
        assert callable(getattr(artwork_service, method_name))
