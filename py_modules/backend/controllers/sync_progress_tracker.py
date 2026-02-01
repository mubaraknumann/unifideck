"""Sync progress tracking for library synchronization.

Tracks overall progress through different sync phases with percentage-based
progress calculation for smooth progress bar updates.
"""

import asyncio
from typing import Dict, Any


class SyncProgress:
    """Track library sync progress with phase-based percentage tracking.
    
    Each sync phase has an allocated percentage range for smooth progress bar updates.
    """
    
    # Phase percentage allocations: (start_pct, end_pct)
    PHASE_RANGES = {
        'idle': (0, 0),
        'fetching': (0, 10),
        'checking_installed': (10, 20),
        'syncing': (20, 40),
        'sgdb_lookup': (40, 55),
        'checking_artwork': (55, 60),
        'artwork': (60, 95),
        'proton_setup': (95, 98),
        'complete': (100, 100),
        'error': (100, 100),
        'cancelled': (100, 100)
    }
    
    def __init__(self):
        self.total_games = 0
        self.synced_games = 0
        self.current_game = {
            "label": None,     # key i18n
            "values": {}       # dynamic values
        }
        self.status = "idle"  # idle, fetching, checking_installed, syncing, sgdb_lookup, checking_artwork, artwork, proton_setup, complete, error, cancelled
        self.error = None

        # Artwork-specific tracking
        self.artwork_total = 0
        self.artwork_synced = 0
        self.current_phase = "sync"  # "sync" or "artwork"

        # Lock for thread-safe updates during parallel downloads
        self._lock = asyncio.Lock()

    async def increment_artwork(self, game_title: str) -> int:
        """Thread-safe artwork counter increment"""
        async with self._lock:
            self.artwork_synced += 1
            self.current_game = {
                "label": "artwork.downloadProgress",
                "values": {
                    "synced": self.artwork_synced,
                    "total": self.artwork_total,
                    "game_title": game_title
                }
            }
            return self.artwork_synced

    def _calculate_progress(self) -> int:
        """Calculate progress based on current phase and its percentage allocation."""
        phase_range = self.PHASE_RANGES.get(self.status, (0, 0))
        start_pct, end_pct = phase_range
        
        # For artwork phase, use artwork counters for sub-progress within the phase range
        if self.status == 'artwork' and self.artwork_total > 0:
            sub_progress = self.artwork_synced / self.artwork_total
            return int(start_pct + (end_pct - start_pct) * sub_progress)
        
        # For other phases, return the start of the phase range
        # (phases transition quickly, so showing phase start is sufficient)
        return start_pct

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': True,
            'total_games': self.total_games,
            'synced_games': self.synced_games,
            'current_game': self.current_game,
            'status': self.status,
            'progress_percent': self._calculate_progress(),
            'error': self.error,
            # Artwork fields
            'artwork_total': self.artwork_total,
            'artwork_synced': self.artwork_synced,
            'current_phase': self.current_phase
        }
