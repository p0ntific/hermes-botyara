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
        self.reply_tasks = set()
        self.reply_locks = {}

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
            self.store.set_account_health(self.name, False)

    async def stop(self):
        self.healthy = False
        for task in list(self.reply_tasks):
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
        if not self.store.account_enabled(self.name):
            return False
        if not self.is_connected() or self.pitch_lock.locked():
            return False
        if self.store.get_cooldown_remaining(self.name) > 0:
            return False
        limit = self.store.daily_limit(self.name, self.cfg.cold_dm_daily_limit)
        return self.store.sent_today(self.name) < limit

    # --- lead feed ---------------------------------------------------------

    async def _lead_feed_handler(self, event):
        if not self.store.account_enabled(self.name):
            return
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

            if not self.store.account_enabled(self.name):
                raise PitchRetryable(f"account {self.name} disabled")

            cooldown_remaining = self.store.get_cooldown_remaining(self.name)
            if cooldown_remaining > 0:
                logger.info(
                    f"[{self.name}] cooldown active for {cooldown_remaining // 60} min, "
                    f"releasing @{target_user} back to the queue"
                )
                raise PitchRetryable(f"cooldown active on {self.name}")

            limit = self.store.daily_limit(
                self.name,
                self.cfg.cold_dm_daily_limit,
            )
            if self.store.sent_today(self.name) >= limit:
                raise PitchRetryable(f"daily limit reached on {self.name}")

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
                notification = (
                    f"[{self.name}] Не удалось отправить холодный питч @{target_user}: "
                    f"{error_text[:200]}\n"
                    f"{sales.manual_message_notice(target_user, pitch_message)}"
                )
                self.store.enqueue_admin_notification(
                    target_user,
                    self.name,
                    self.cfg.manager_username,
                    "manual_required",
                    notification,
                    "manual_required",
                )
                if not self.store.claim_admin_notification(
                    target_user,
                    "manual_required",
                ):
                    return "failed"
                try:
                    await self.notifier.notify(
                        notification,
                        fallback_username=self.cfg.manager_username,
                        preferred_client=self.client,
                    )
                    self.store.complete_admin_notification(
                        target_user,
                        "manual_required",
                    )
                except Exception as notify_error:
                    self.store.fail_admin_notification(
                        target_user,
                        "manual_required",
                        notify_error,
                    )
                    logger.error(
                        f"[{self.name}] failed to notify about manual pitch "
                        f"for @{target_user}: {notify_error}"
                    )
                return "failed"

            self.store.add_contacted(target_user, self.name, "sent")
            self.store.finish_queue(target_user, "done")
            self.store.record_message(target_user, self.name, "out", pitch_message, meta={"kind": "pitch"})
            logger.info(f"[{self.name}] successfully pitched @{target_user}")
            return "sent"

    # --- private replies -----------------------------------------------------

    async def _pm_handler(self, event):
        if not event.is_private or not self.store.account_enabled(self.name):
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

        state = self.pending_reply_tasks.get(target_key)
        if (
            state
            and not state["task"].done()
            and not state.get("processing")
        ):
            state["task"].cancel()

        task_state = {"processing": False, "task": None}
        task_state["task"] = asyncio.create_task(
            self.process_private_reply(
                event.chat_id,
                sender_username,
                target_key,
                task_state=task_state,
            )
        )
        self.reply_tasks.add(task_state["task"])
        task_state["task"].add_done_callback(self.reply_tasks.discard)
        self.pending_reply_tasks[target_key] = task_state

    async def get_history_text(self, chat_id, sender_username, limit=20):
        messages = await self.client.get_messages(chat_id, limit=limit)
        messages.reverse()
        return sales.format_history(messages, sender_username, self.settings.product_name)

    async def process_private_reply(
        self,
        chat_id,
        sender_username,
        target_key,
        task_state=None,
    ):
        reply_lock = None
        lock_acquired = False
        try:
            await asyncio.sleep(self.settings.incoming_reply_debounce_seconds)
            if task_state is not None:
                task_state["processing"] = True
            reply_lock = self.reply_locks.setdefault(target_key, asyncio.Lock())
            await reply_lock.acquire()
            lock_acquired = True

            if not self.store.account_enabled(self.name):
                return

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

            if (
                action == "send_reply"
                and current_reply_count >= self.settings.reply_daily_cap
            ):
                logger.info(
                    f"[{self.name}] daily reply cap reached for {sender_username}; "
                    "staying silent until tomorrow"
                )
                return

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

            stored_decision = ai_response
            manager_msg = None
            already_notified = False
            if should_notify_manager:
                if action == "handoff_to_manager":
                    stored_decision = dict(ai_response)
                    stored_decision["status"] = "notification_pending"
                manager_msg = sales.manager_notification_text(
                    sender_username,
                    ai_response,
                    notification_history,
                    account=self.name,
                )
                already_notified = (
                    self.store.apply_decision_and_enqueue_notification(
                        target_key,
                        stored_decision,
                        self.name,
                        self.cfg.manager_username,
                        manager_msg,
                        status or "manual_review",
                    )
                )

            reply_sent = False
            reply_error = None
            if reply_text.strip():
                try:
                    await self.client.send_message(chat_id, reply_text)
                    reply_sent = True
                except errors.FloodWaitError as e:
                    seconds = int(getattr(e, "seconds", 0) or 0)
                    reply_error = f"FloodWait {seconds}s"
                    logger.error(
                        f"[{self.name}] FloodWait when replying to "
                        f"{sender_username}: {seconds}s"
                    )
                    self.store.activate_cooldown(
                        self.name,
                        seconds + self.settings.flood_wait_extra_seconds,
                        f"FloodWait while replying to @{sender_username}",
                    )
                except Exception as e:
                    reply_error = str(e)
                    logger.error(f"[{self.name}] error replying to {sender_username}: {e}")
                if reply_sent:
                    self.store.record_message(
                        target_key,
                        self.name,
                        "out",
                        reply_text,
                        meta={"kind": "reply"},
                    )
                    logger.info(f"[{self.name}] auto-replied to {sender_username}")
            else:
                logger.info(f"[{self.name}] AI action for {sender_username} has no client reply: {action}")

            if action == "send_reply" and not reply_sent:
                return

            if not should_notify_manager:
                self.store.apply_decision(
                    target_key,
                    stored_decision,
                    replied=reply_sent,
                )

            if should_notify_manager and not already_notified:
                delivery_note = ""
                if action == "handoff_to_manager" and not reply_sent:
                    delivery_note = (
                        f"⚠️ Ответ клиенту НЕ доставлен "
                        f"({reply_error or 'ошибка отправки'}). "
                        f"Напишите клиенту сами: \"{reply_text}\""
                    )
                try:
                    warm_history = await self.get_history_text(chat_id, sender_username, limit=30)
                    notification_history = warm_history or notification_history
                except Exception as e:
                    logger.warning(
                        f"[{self.name}] failed to refresh notification history for "
                        f"{sender_username}: {e}"
                    )
                manager_msg = sales.manager_notification_text(
                    sender_username,
                    ai_response,
                    notification_history,
                    account=self.name,
                    delivery_note=delivery_note,
                )
                self.store.update_admin_notification_message(
                    target_key,
                    action,
                    manager_msg,
                )
                if not self.store.claim_admin_notification(target_key, action):
                    return
                try:
                    channel = await self.notifier.notify(
                        manager_msg,
                        fallback_username=self.cfg.manager_username,
                        preferred_client=self.client,
                    )
                    self.store.complete_admin_notification(target_key, action)
                    logger.info(
                        f"[{self.name}] delivered manager notification via "
                        f"{channel} about {action} for {sender_username}"
                    )
                except Exception as e:
                    self.store.fail_admin_notification(target_key, action, e)
                    logger.error(
                        f"[{self.name}] failed to notify admin group about warm lead {sender_username}: {e}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.name}] private reply task failed for {sender_username}: {e}")
        finally:
            if lock_acquired:
                reply_lock.release()
            if task_state is not None and self.pending_reply_tasks.get(target_key) is task_state:
                self.pending_reply_tasks.pop(target_key, None)
