import asyncio
import logging

from . import sales
from .worker import PitchRetryable

logger = logging.getLogger(__name__)


class Dispatcher:
    """Feeds the shared durable lead queue to whichever account has capacity.

    Selection is least-recently-dispatched among healthy accounts that are not in
    cooldown and still have daily budget, so load spreads evenly and a flood-limited
    account never blocks the others.
    """

    POLL_INTERVAL_SECONDS = 30

    def __init__(self, store, workers, settings, notifier=None):
        self.store = store
        self.workers = workers
        self.settings = settings
        self.notifier = notifier
        self.wake = asyncio.Event()
        self._inflight = set()
        self._busy_accounts = set()

    def notify_new_lead(self):
        self.wake.set()

    def pick_worker(self):
        candidates = [
            w for w in self.workers
            if w.name not in self._busy_accounts and w.has_capacity()
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda w: self.store.last_dispatch_at(w.name))

    async def run(self):
        logger.info(f"Dispatcher started with accounts: {[w.name for w in self.workers]}")
        while True:
            try:
                dispatched = await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dispatcher iteration failed")
                dispatched = False
            if not dispatched:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.POLL_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()

    async def _dispatch_once(self):
        if self.store.pending_count() == 0:
            return False
        worker = self.pick_worker()
        if worker is None:
            logger.info("Leads pending, but no account has capacity right now")
            return False

        item = self.store.claim_next_pending(worker.name, self.settings.max_pitch_attempts)
        if item is None:
            return False
        if item["status"] == "failed":
            await self._give_up(item)
            return True

        self.store.mark_dispatched(worker.name)
        self._busy_accounts.add(worker.name)
        task = asyncio.create_task(self._run_pitch(worker, item))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return True

    async def _run_pitch(self, worker, item):
        lead_key = item["lead_key"]
        try:
            await worker.process_lead(item)
        except PitchRetryable as e:
            logger.info(f"Releasing lead @{lead_key} back to queue: {e}")
            self.store.release_lead(lead_key, error=str(e)[:300])
        except asyncio.CancelledError:
            self.store.release_lead(lead_key, error="dispatcher shutdown")
            raise
        except Exception as e:
            logger.exception(f"Pitch task for @{lead_key} crashed on {worker.name}")
            self.store.release_lead(lead_key, error=str(e)[:300])
        finally:
            self._busy_accounts.discard(worker.name)
            self.wake.set()

    async def _give_up(self, item):
        lead_key = item["lead_key"]
        logger.error(
            f"Lead @{lead_key} exhausted {self.settings.max_pitch_attempts} pitch attempts, "
            "marking manual_required"
        )
        self.store.add_contacted(lead_key, None, "manual_required")
        if self.notifier:
            try:
                await self.notifier.notify(
                    f"⚠️ Лид @{lead_key} не удалось обработать автоматически "
                    f"({self.settings.max_pitch_attempts} попыток). "
                    + sales.manual_message_notice(lead_key, "")
                )
            except Exception:
                logger.exception("Failed to notify about given-up lead")
