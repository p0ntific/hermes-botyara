import asyncio
import logging

from dotenv import load_dotenv

from .config import load_settings, load_accounts, build_llm_routes
from .dispatcher import Dispatcher
from .llm import LLMRouter
from .notify import AdminNotifier
from .sales import SalesBrain
from .store import Store
from .worker import AccountWorker, FATAL_ERRORS

logger = logging.getLogger(__name__)

SUPERVISOR_BACKOFF_START = 15
SUPERVISOR_BACKOFF_MAX = 600


def recover_orphaned_notifications(store, accounts, default_manager):
    """Recreate handoff outbox rows left by a crash from an older build."""
    managers = {
        account.name: account.manager_username or default_manager
        for account in accounts
    }
    recovered = 0
    for lead in store.orphaned_notification_pending():
        lead_key = lead["lead_key"]
        account = lead.get("account")
        recipient = managers.get(account, default_manager)
        transcript = store.get_transcript(lead_key, limit=30)
        history_lines = []
        for entry in transcript:
            text = entry.get("text")
            if not text:
                continue
            speaker = "Клиент" if entry["direction"] == "in" else "Бот"
            history_lines.append(f"{speaker}: {text}")
        history = "\n".join(history_lines) or "<история недоступна>"
        message = (
            f"🚨 ВОССТАНОВЛЕННЫЙ ТЕПЛЫЙ ЛИД @{lead_key}\n\n"
            f"Аккаунт: {account or '-'}\n"
            "Предыдущий процесс сохранил handoff, но не успел создать "
            "уведомление. Проверьте диалог и свяжитесь с лидом.\n\n"
            f"История переписки:\n{history}"
        )
        store.enqueue_admin_notification(
            lead_key,
            account,
            recipient,
            "handoff_to_manager",
            message,
            "warm_notified",
        )
        recovered += 1
    if recovered:
        logger.warning(
            f"Recovered {recovered} orphaned handoff notification(s)"
        )
    return recovered


async def supervise_worker(worker, notifier):
    """Keep one account alive independently of the others.

    Transient failures restart with exponential backoff; fatal auth errors park the
    account (admin notified) while the rest of the pool keeps working.
    """
    backoff = SUPERVISOR_BACKOFF_START
    while True:
        started = asyncio.get_event_loop().time()
        try:
            await worker.run()
            logger.warning(f"[{worker.name}] client disconnected, restarting in {backoff}s")
        except asyncio.CancelledError:
            await worker.stop()
            raise
        except FATAL_ERRORS as e:
            worker.store.set_account_health(worker.name, False, error=str(e)[:300])
            logger.critical(f"[{worker.name}] fatal session error, account disabled: {e}")
            try:
                await notifier.notify(
                    f"🚨 Аккаунт {worker.name} отключен: {type(e).__name__}: {e}\n"
                    "Замените SESSION в конфигурации и перезапустите."
                )
            except Exception:
                logger.exception("Failed to send fatal-account notification")
            return
        except Exception as e:
            worker.store.set_account_health(worker.name, False, error=str(e)[:300])
            logger.exception(f"[{worker.name}] worker crashed, restarting in {backoff}s")
        await worker.stop()
        if asyncio.get_event_loop().time() - started > 300:
            backoff = SUPERVISOR_BACKOFF_START
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, SUPERVISOR_BACKOFF_MAX)


async def retry_admin_notifications(store, notifier, workers, retry_seconds):
    """Deliver durable outbox entries until an admin channel confirms success."""
    while True:
        for item in store.claim_pending_admin_notifications():
            try:
                preferred_client = next(
                    (
                        worker.client
                        for worker in workers
                        if worker.name == item["account"] and worker.is_connected()
                    ),
                    None,
                )
                channel = await notifier.notify(
                    item["message"],
                    fallback_username=item.get("recipient"),
                    preferred_client=preferred_client,
                )
                store.complete_admin_notification(
                    item["lead_key"],
                    item["action"],
                )
                logger.info(
                    f"[{item['account'] or '-'}] delivered pending admin notification "
                    f"for {item['lead_key']} via {channel}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                store.fail_admin_notification(
                    item["lead_key"],
                    item["action"],
                    e,
                )
                logger.error(
                    f"[{item['account'] or '-'}] pending admin notification failed "
                    f"for {item['lead_key']}: {e}"
                )
        await asyncio.sleep(retry_seconds)


async def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    settings = load_settings()
    accounts = load_accounts(settings)
    if not accounts:
        logger.error(
            "No Telegram accounts configured. Provide accounts.yaml (ACCOUNTS_FILE) "
            "or legacy API_ID/API_HASH/SESSION env vars."
        )
        return

    store = Store(settings.db_path)
    store.migrate_legacy_json(settings.legacy_json_path, [a.name for a in accounts])
    for account in accounts:
        store.ensure_account(account.name)
    recover_orphaned_notifications(
        store,
        accounts,
        settings.manager_username,
    )
    # Nothing can legitimately be mid-pitch at startup: reclaim orphaned claims.
    requeued = store.requeue_stuck(older_than_seconds=0)
    if requeued:
        logger.info(f"Requeued {requeued} leads stuck in processing from a previous run")

    routes = build_llm_routes()
    router = LLMRouter(routes, on_call=store.record_llm_call)
    for task in routes:
        logger.info(f"LLM route for {task}: {router.describe(task)}")

    brain = SalesBrain(router, settings)

    workers = []
    notifier = AdminNotifier(
        settings.bot_token,
        settings.bot_chat_id,
        settings.proxy_url,
        client_provider=lambda: [w.client for w in workers if w.is_connected()],
    )

    dispatcher = Dispatcher(store, workers, settings, notifier=notifier)
    for account in accounts:
        workers.append(
            AccountWorker(
                account,
                settings,
                store,
                brain,
                notifier,
                on_lead_enqueued=dispatcher.notify_new_lead,
            )
        )

    logger.info(
        f"Hermes started: accounts={[a.name for a in accounts]}, "
        f"pending_leads={store.pending_count()}"
    )

    tasks = [asyncio.create_task(supervise_worker(w, notifier), name=f"worker:{w.name}") for w in workers]
    tasks.append(asyncio.create_task(dispatcher.run(), name="dispatcher"))
    tasks.append(
        asyncio.create_task(
            retry_admin_notifications(
                store,
                notifier,
                workers,
                settings.notify_retry_interval_seconds,
            ),
            name="admin-notification-retry",
        )
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()
        logger.info("Hermes stopped")


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
