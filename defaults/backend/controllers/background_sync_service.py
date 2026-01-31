"""Background sync service.

Extracted from main.py to reduce size; behavior should remain identical.
"""

import asyncio

import decky

logger = decky.logger


class BackgroundSyncService:
    """Background service that syncs libraries every 5 minutes"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.running = False
        self.task = None

    async def start(self):
        """Start background sync"""
        if self.running:
            logger.warning("Background sync already running")
            return

        self.running = True
        self.task = asyncio.create_task(self._sync_loop())
        logger.info("Background sync service started")

    async def stop(self):
        """Stop background sync"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Background sync service stopped")

    async def _sync_loop(self):
        """Main sync loop - sync game lists only, no artwork"""
        while self.running:
            try:
                # Only sync game lists, don't fetch artwork in background
                await self.plugin.sync_libraries(fetch_artwork=False)
                await asyncio.sleep(300)  # 5 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
                await asyncio.sleep(60)  # Retry in 1 minute on error
