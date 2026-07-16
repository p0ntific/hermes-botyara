import asyncio
import random
import logging

from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

from . import sales
from .sales import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

_FATAL_ERROR_NAMES = (
    "AuthKeyError",
    "AuthKeyUnregisteredError",
    "AuthKeyDuplicatedError",
    "SessionRevokedError",
    "SessionExpiredError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "PhoneNumberBannedError",
)
FATAL_ERRORS = tuple(
    cls for cls in (getattr(errors, name, None) for name in _FATAL_ERROR_NAMES) if cls
)


class PitchRetryable(Exception):
    """Send failed for account-level reasons; the lead should go back to the queue."""


class AccountWorker:
    """One Telegram userbot account: pitches assigned leads, drives its own dialogs."""

    def __init__(self, cfg, settings, store, brain, notifier, on_lead_enqueued=None):
        self.cfg = cfg
        self.name = cfg.name
        self.settings = settings
        self.store = store
        self.brain = brain
        self.notifier = notifier
        self.on_lead_enqueued = on_lead_enqueued or (lambda: None)

        self.client = None
        self.healthy = False
        self.pitch_lock = asyncio.Lock()
        self.pending_reply_tasks = {}

    # --- lifecycle -------------------------------------------------------

    def build_client(self):
        session = StringSession(self.cfg.session)
        if session.server_address and session.port != 443:
            session.set_dc(session.dc_id, session.server_address, 443)
        return TelegramClient(
            session,
            self.cfg.api_id,
            self.cfg.api_hash,
            proxy=self.cfg.proxy,
        )

    async def run(self):
        self.client = self.build_client()
        self._register_handlers()
        await self.client.start()
        self.healthy = True
        self.store.set_account_health(self.name, True)
        logger.info(f"[{self.name}] account connected, listening")
        try:
            await self.client.run_until_disconnected()
        finally:
            self.healthy = False

    async def stop(self):
        self.healthy = False
        for task in list(self.pending_reply_tasks.values()):
            task.cancel()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def _register_handlers(self):
        if self.settings.target_chat:
            self.client.add_event_handler(
                self._lead_feed_handler, events.NewMessage(chats=self.settings.target_chat)
            )
        self.client.add_event_handler(self._pm_handler, events.NewMessage(incoming=True))

    # --- capacity ---------------------------------------------------------

    def is_connected(self):
        return bool(self.client) and self.healthy and self.client.is_connected()

    def has_capacity(self):
        if not self.is_connected() or self.pitch_lock.locked():
            return False
        if self.store.get_cooldown_remaining(self.name) > 0:
            return False
        return self.store.sent_today(self.name) < self.cfg.cold_dm_daily_limit

    # --- lead feed ---------------------------------------------------------

    async def _lead_feed_handler(self, event):
        text = event.raw_text
        target_user = sales.extract_target_username(text)
        if not target_user:
            logger.warning(f"[{self.name}] could not extract target username from lead header. Skipping lead.")
            return

        lead_context = sales.extract_lead_context(text)
        if self.store.enqueue_lead(target_user, lead_context):
            logger.info(f"[{self.name}] queued new lead @{target_user}")
            self.on_lead_enqueued()
        else:
            logger.debug(f"[{self.name}] lead @{target_user} already known, ignoring duplicate")

    # --- cold pitch ---------------------------------------------------------

    async def process_lead(self, queue_item):
        """Pitch one claimed lead. Raises PitchRetryable for account-level failures."""
        target_user = queue_item["lead_key"]
        lead_context = queue_item.get("context") or ""

        if self.store.get_lead(target_user):
            logger.info(f"[{self.name}] lead @{target_user} already contacted, finishing duplicate queue item")
            self.store.finish_queue(target_user, "duplicate")
            return "duplicate"

        async with self.pitch_lock:
            pitch_message = await asyncio.to_thread(
                self.brain.build_pitch_message, target_user, lead_context
            )
            if pitch_message == "SKIP":
                logger.info(f"[{self.name}] skipped lead @{target_user}: AI решил SKIP.")
                self.store.add_contacted(target_user, self.name, "skipped")
                self.store.finish_queue(target_user, "skipped")
                return "skipped"

            delay = random.randint(
                self.settings.send_delay_min_seconds, self.settings.send_delay_max_seconds
            )
            logger.info(f"[{self.name}] sleeping for {delay}s before pitching @{target_user}...")
            await asyncio.sleep(delay)

            cooldown_remaining = self.store.get_cooldown_remaining(self.name)
            if cooldown_remaining > 0:
                logger.info(
                    f"[{self.name}] cooldown active for {cooldown_remaining // 60} min, "
                    f"releasing @{target_user} back to the queue"
                )
                raise PitchRetryable(f"cooldown active on {self.name}")

            try:
                # Persist before the network send so a crash after Telegram accepts
                # the message cannot cause another account to cold-pitch this lead.
                self.store.add_contacted(target_user, self.name, "pitching")
                await self.client.send_message(target_user, pitch_message)
            except errors.FloodWaitError as e:
                self.store.remove_lead_if_status(target_user, self.name, "pitching")
                logger.error(f"[{self.name}] flood wait error. Must wait {e.seconds} seconds.")
                self.store.activate_cooldown(
                    self.name,
                    e.seconds + self.settings.flood_wait_extra_seconds,
                    f"FloodWait while pitching @{target_user}",
                )
                raise PitchRetryable(f"FloodWait {e.seconds}s on {self.name}")
            except Exception as e:
                error_text = str(e)
                if "Too many requests" in error_text or "A wait of" in error_text:
                    self.store.remove_lead_if_status(target_user, self.name, "pitching")
                    logger.error(f"[{self.name}] failed to send pitch to @{target_user}: {error_text}")
                    self.store.activate_cooldown(
                        self.name,
                        self.settings.generic_limit_cooldown_seconds,
                        f"Telegram limit while pitching @{target_user}: {error_text}",
                    )
                    raise PitchRetryable(f"Telegram limit on {self.name}")
                logger.error(f"[{self.name}] failed to send pitch to @{target_user}: {e}")
                self.store.add_contacted(target_user, self.name, "manual_required")
                self.store.finish_queue(target_user, "failed", error=error_text[:300])
                await self.notifier.notify(
                    f"[{self.name}] {sales.manual_message_notice(target_user, pitch_message)}"
                )
                return "failed"

            self.store.add_contacted(target_user, self.name, "sent")
            self.store.finish_queue(target_user, "done")
            self.store.record_message(target_user, self.name, "out", pitch_message, meta={"kind": "pitch"})
            logger.info(f"[{self.name}] successfully pitched @{target_user}")
            return "sent"

    # --- private replies -----------------------------------------------------

    async def _pm_handler(self, event):
        if not event.is_private:
            return

        sender = await event.get_sender()
        sender_id = str(sender.id) if sender else ""
        sender_username = sender.username if sender and sender.username else sender_id

        lead = self.store.find_lead(sender_username, sender_id)
        if lead is None:
            return

        if lead["account"] is None:
            # Lead migrated from the single-account era: bind it to whichever
            # account actually received the reply.
            self.store.claim_lead_account(lead["lead_key"], self.name)
        elif lead["account"] != self.name:
            return

        target_key = lead["lead_key"]
        if sender_id and not lead.get("peer_id"):
            try:
                self.store.set_lead_peer(target_key, int(sender_id))
            except (TypeError, ValueError):
                pass

        logger.info(f"[{self.name}] queued reply from {sender_username}: {event.raw_text}")
        self.store.record_message(target_key, self.name, "in", event.raw_text)

        existing_task = self.pending_reply_tasks.get(target_key)
        if existing_task and not existing_task.done():
            existing_task.cancel()

        self.pending_reply_tasks[target_key] = asyncio.create_task(
            self.process_private_reply(event.chat_id, sender_username, target_key)
        )

    async def get_history_text(self, chat_id, sender_username, limit=20):
        messages = await self.client.get_messages(chat_id, limit=limit)
        messages.reverse()
        return sales.format_history(messages, sender_username, self.settings.product_name)

    async def process_private_reply(self, chat_id, sender_username, target_key):
        try:
            await asyncio.sleep(self.settings.incoming_reply_debounce_seconds)

            lead = self.store.get_lead(target_key)
            if lead is None:
                return
            if lead.get("status") in TERMINAL_STATUSES:
                logger.info(
                    f"[{self.name}] skipping auto-reply for {sender_username}: "
                    f"terminal status {lead.get('status')}"
                )
                return

            current_reply_count = self.store.reset_reply_count_if_new_day(target_key)
            if current_reply_count >= 10:
                return

            try:
                await self.client.send_read_acknowledge(chat_id)
            except Exception:
                pass

            try:
                history_text = await self.get_history_text(chat_id, sender_username, limit=14)
            except Exception as e:
                logger.error(f"[{self.name}] failed to fetch history for {sender_username}: {e}")
                history_text = f"Клиент (@{sender_username}): <не удалось загрузить историю>"

            async with self.client.action(chat_id, "typing"):
                ai_response = await asyncio.to_thread(
                    self.brain.generate_conversational_reply,
                    history_text,
                    target_key,
                    self.cfg.manager_username,
                )

            if ai_response is None or "reply_text" not in ai_response:
                logger.error(f"[{self.name}] failed to generate JSON reply for {sender_username}")
                return

            reply_text = str(ai_response.get("reply_text") or "")
            action = ai_response.get("action", "send_reply")
            status = ai_response.get("status")
            should_notify_manager = bool(ai_response.get("notify_manager"))
            notification_history = history_text

            self.store.record_message(
                target_key,
                self.name,
                "event",
                None,
                meta={
                    "stage": ai_response.get("stage"),
                    "action": action,
                    "confidence": ai_response.get("confidence"),
                    "reason": ai_response.get("reason"),
                    "model": ai_response.get("model"),
                },
            )

            if reply_text.strip():
                try:
                    await self.client.send_message(chat_id, reply_text)
                except errors.FloodWaitError as e:
                    logger.error(f"[{self.name}] FloodWait when replying to {sender_username}: {e.seconds}s")
                    return
                except Exception as e:
                    logger.error(f"[{self.name}] error replying to {sender_username}: {e}")
                    return
                self.store.record_message(target_key, self.name, "out", reply_text, meta={"kind": "reply"})
                logger.info(f"[{self.name}] auto-replied to {sender_username}")
            else:
                logger.info(f"[{self.name}] AI action for {sender_username} has no client reply: {action}")

            already_notified = self.store.apply_decision(
                target_key, ai_response, replied=bool(reply_text.strip())
            )

            if should_notify_manager and not already_notified:
                try:
                    warm_history = await self.get_history_text(chat_id, sender_username, limit=30)
                    notification_history = warm_history or notification_history
                    manager_msg = sales.manager_notification_text(
                        sender_username, ai_response, notification_history, account=self.name
                    )
                    await self.notifier.notify(manager_msg)
                    self.store.mark_manager_notified(target_key, status)
                    logger.info(f"[{self.name}] notified admin group about {action} for {sender_username}")
                except Exception as e:
                    logger.error(
                        f"[{self.name}] failed to notify admin group about warm lead {sender_username}: {e}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.name}] private reply task failed for {sender_username}: {e}")
        finally:
            current_task = asyncio.current_task()
            if self.pending_reply_tasks.get(target_key) is current_task:
                self.pending_reply_tasks.pop(target_key, None)
