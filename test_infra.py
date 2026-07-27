import asyncio
import datetime
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


class FakeFloodWaitError(Exception):
    def __init__(self, seconds=1):
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


if "requests" not in sys.modules:
    sys.modules["requests"] = types.ModuleType("requests")

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

if "openai" not in sys.modules:
    openai = types.ModuleType("openai")
    openai.OpenAI = lambda *args, **kwargs: object()
    sys.modules["openai"] = openai

if "telethon" not in sys.modules:
    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = object
    telethon.events = types.SimpleNamespace()
    telethon.errors = types.SimpleNamespace(FloodWaitError=FakeFloodWaitError)
    sys.modules["telethon"] = telethon

if "telethon.sessions" not in sys.modules:
    sys.modules["telethon.sessions"] = types.SimpleNamespace(StringSession=object)

from hermes import config
from hermes.app import recover_orphaned_notifications
from hermes.dashboard import Handler, funnel_svg, qr_svg
from hermes.dispatcher import Dispatcher
from hermes.llm import LLMRouter, LLMUnavailable
from hermes.notify import AdminNotifier
from hermes.schedule import is_outreach_allowed, seconds_until_outreach
from hermes.store import Store
from hermes.worker import AccountWorker
from test_sales_flow import make_settings


class TempStoreMixin:
    def make_store(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        os.unlink(tmp.name)
        store = Store(tmp.name)
        self.addCleanup(lambda: (store.close(), os.path.exists(tmp.name) and os.unlink(tmp.name)))
        return store


class AccountsConfigTests(unittest.TestCase):
    def test_yaml_accounts_with_env_interpolation(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                """
accounts:
  - name: main
    api_id: 111
    api_hash: hash-a
    session: ${TEST_SESSION_MAIN}
    manager_username: andrew_pontific
    cold_dm_daily_limit: 3
  - name: second
    api_id: 222
    api_hash: hash-b
    session: inline-session
    proxy: socks5h://127.0.0.1:9052
  - name: disabled_one
    api_id: 333
    api_hash: hash-c
    session: s
    enabled: false
"""
            )
            path = f.name
        self.addCleanup(os.unlink, path)

        env = {
            "ACCOUNTS_FILE": path,
            "TEST_SESSION_MAIN": "secret-session",
            "PROXY_URL": "socks5h://127.0.0.1:9051",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            settings = config.load_settings()
            accounts = config.load_accounts(settings)

        self.assertEqual([a.name for a in accounts], ["main", "second"])
        self.assertEqual(accounts[0].session, "secret-session")
        self.assertEqual(accounts[0].manager_username, "andrew_pontific")
        self.assertEqual(accounts[0].cold_dm_daily_limit, 3)
        self.assertEqual(accounts[0].proxy_url, "socks5h://127.0.0.1:9051")

        self.assertEqual(accounts[1].manager_username, settings.manager_username)
        self.assertEqual(accounts[1].proxy_url, "socks5h://127.0.0.1:9052")
        self.assertEqual(accounts[1].proxy, {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 9052})

    def test_legacy_env_fallback(self):
        env = {
            "ACCOUNTS_FILE": "/nonexistent/accounts.yaml",
            "API_ID": "42",
            "API_HASH": "legacy-hash",
            "SESSION": "legacy-session",
            "PROXY_URL": "socks5h://127.0.0.1:9051",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            settings = config.load_settings()
            accounts = config.load_accounts(settings)

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].name, "main")
        self.assertEqual(accounts[0].api_id, 42)
        self.assertEqual(accounts[0].proxy_url, "socks5h://127.0.0.1:9051")

    def test_duplicate_account_names_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                """
accounts:
  - {name: main, api_id: 1, api_hash: a, session: s}
  - {name: main, api_id: 2, api_hash: b, session: t}
"""
            )
            path = f.name
        self.addCleanup(os.unlink, path)

        with mock.patch.dict(os.environ, {"ACCOUNTS_FILE": path}, clear=False):
            settings = config.load_settings()
            with self.assertRaises(ValueError):
                config.load_accounts(settings)


class OutreachScheduleTests(unittest.TestCase):
    def test_moscow_quiet_hours_wrap_across_midnight(self):
        settings = make_settings(
            outreach_quiet_start_hour=20,
            outreach_quiet_end_hour=9,
        )
        timezone = datetime.timezone(datetime.timedelta(hours=3))

        self.assertTrue(
            is_outreach_allowed(
                settings,
                datetime.datetime(2026, 7, 25, 9, 0, tzinfo=timezone),
            )
        )
        self.assertTrue(
            is_outreach_allowed(
                settings,
                datetime.datetime(2026, 7, 25, 19, 59, tzinfo=timezone),
            )
        )
        self.assertFalse(
            is_outreach_allowed(
                settings,
                datetime.datetime(2026, 7, 25, 20, 0, tzinfo=timezone),
            )
        )
        self.assertFalse(
            is_outreach_allowed(
                settings,
                datetime.datetime(2026, 7, 26, 8, 59, tzinfo=timezone),
            )
        )

    def test_seconds_until_outreach_returns_moscow_nine_am(self):
        settings = make_settings(
            outreach_quiet_start_hour=20,
            outreach_quiet_end_hour=9,
        )
        timezone = datetime.timezone(datetime.timedelta(hours=3))

        self.assertEqual(
            seconds_until_outreach(
                settings,
                datetime.datetime(2026, 7, 25, 23, 30, tzinfo=timezone),
            ),
            9 * 60 * 60 + 30 * 60,
        )


class DashboardSecurityTests(unittest.TestCase):
    def test_cookie_token_authentication(self):
        request = types.SimpleNamespace(
            headers={"Cookie": "hermes=secret"},
            dashboard=types.SimpleNamespace(token="secret"),
        )

        self.assertTrue(Handler.authorized(request))
        request.headers = {"Cookie": "hermes=wrong"}
        self.assertFalse(Handler.authorized(request))

    def test_qr_is_rendered_locally(self):
        svg = qr_svg("tg://login?token=secret")
        self.assertIn("<svg", svg)
        self.assertNotIn("secret", svg)

    def test_funnel_chart_contains_all_four_series(self):
        bucket = {
            "date": "2026-07-01",
            "read_cr": 80.0,
            "first_reply_cr": 60.0,
            "second_reply_cr": 40.0,
            "recommendation_cr": 20.0,
        }
        svg = funnel_svg([bucket])

        self.assertEqual(svg.count('class="series"'), 4)
        self.assertIn("CR в рекомендацию · 01.07.2026: 20%", svg)


class LLMRoutesConfigTests(unittest.TestCase):
    BASE_ENV = {
        "LLM_PROVIDER": "",
        "LLM_MODEL": "",
        "LLM_FALLBACK_PROVIDER": "",
        "LLM_FALLBACK_MODEL": "",
        "OPENROUTER_API_KEY": "",
        "OPENROUTER_MODEL": "",
        "YANDEX_CLOUD_API_KEY": "",
        "YANDEX_CLOUD_FOLDER": "",
        "YANDEX_CLOUD_MODEL": "",
        "OPENAI_API_KEY": "",
        "LLM_MODEL_PITCH": "",
        "LLM_MODEL_CLASSIFY": "",
        "LLM_MODEL_REPLY": "",
        "LLM_PROVIDER_PITCH": "",
        "LLM_PROVIDER_CLASSIFY": "",
        "LLM_PROVIDER_REPLY": "",
    }

    def test_openrouter_with_yandex_fallback_and_per_task_model(self):
        env = {
            **self.BASE_ENV,
            "LLM_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "sk-or-xxx",
            "LLM_MODEL_CLASSIFY": "anthropic/claude-haiku-4.5",
            "LLM_FALLBACK_PROVIDER": "yandex",
            "YANDEX_CLOUD_API_KEY": "y-key",
            "YANDEX_CLOUD_FOLDER": "folder1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            routes = config.build_llm_routes()

        self.assertEqual(set(routes), {"pitch", "classify", "reply"})
        pitch = routes["pitch"]
        self.assertEqual(len(pitch), 2)
        self.assertEqual(pitch[0].provider, "openrouter")
        self.assertEqual(pitch[0].resolved_model(), config.DEFAULT_OPENROUTER_MODEL)
        self.assertEqual(pitch[1].provider, "yandex")
        self.assertEqual(pitch[1].resolved_model(), "gpt://folder1/yandexgpt-5.1/latest")

        classify = routes["classify"]
        self.assertEqual(classify[0].resolved_model(), "anthropic/claude-haiku-4.5")

    def test_yandex_legacy_env_still_works(self):
        env = {
            **self.BASE_ENV,
            "YANDEX_CLOUD_API_KEY": "y-key",
            "YANDEX_CLOUD_FOLDER": "folderZ",
            "YANDEX_CLOUD_MODEL": "yandexgpt-5.1/latest",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            routes = config.build_llm_routes()

        for task in ("pitch", "classify", "reply"):
            endpoint = routes[task][0]
            self.assertEqual(endpoint.provider, "yandex")
            self.assertEqual(endpoint.resolved_model(), "gpt://folderZ/yandexgpt-5.1/latest")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            config.build_endpoint("nonexistent")


class _FakeCompletionClient:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls += 1
        result = self.behavior(self.calls)
        if isinstance(result, Exception):
            raise result
        message = types.SimpleNamespace(content=result)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class LLMRouterTests(unittest.TestCase):
    def endpoints(self):
        primary = config.LLMEndpoint(provider="openrouter", base_url="https://p", api_key="k1", model="model-a")
        fallback = config.LLMEndpoint(provider="yandex", base_url="https://f", api_key="k2", model="model-b", yandex_folder="dir")
        return primary, fallback

    def test_falls_back_to_second_endpoint(self):
        primary, fallback = self.endpoints()
        clients = {
            "openrouter": _FakeCompletionClient(lambda n: RuntimeError("boom")),
            "yandex": _FakeCompletionClient(lambda n: "ответ"),
        }
        calls = []
        router = LLMRouter(
            {"pitch": [primary, fallback]},
            on_call=lambda **kw: calls.append(kw),
            attempts_per_endpoint=2,
            client_factory=lambda e: clients[e.provider],
        )

        result = router.chat("pitch", [{"role": "user", "content": "hi"}])

        self.assertEqual(result.text, "ответ")
        self.assertEqual(result.provider, "yandex")
        self.assertEqual(result.model, "gpt://dir/model-b")
        self.assertEqual(clients["openrouter"].calls, 2)
        self.assertEqual(clients["yandex"].calls, 1)
        self.assertEqual([c["ok"] for c in calls], [False, False, True])

    def test_raises_when_all_endpoints_fail(self):
        primary, fallback = self.endpoints()
        clients = {
            "openrouter": _FakeCompletionClient(lambda n: RuntimeError("a")),
            "yandex": _FakeCompletionClient(lambda n: RuntimeError("b")),
        }
        router = LLMRouter(
            {"pitch": [primary, fallback]},
            attempts_per_endpoint=1,
            client_factory=lambda e: clients[e.provider],
        )

        with self.assertRaises(LLMUnavailable):
            router.chat("pitch", [{"role": "user", "content": "hi"}])

    def test_retries_transient_error_on_same_endpoint(self):
        primary, _ = self.endpoints()
        client = _FakeCompletionClient(lambda n: RuntimeError("flaky") if n == 1 else "ok")
        router = LLMRouter(
            {"classify": [primary]},
            attempts_per_endpoint=2,
            client_factory=lambda e: client,
        )

        result = router.chat("classify", [{"role": "user", "content": "hi"}])

        self.assertEqual(result.text, "ok")
        self.assertEqual(client.calls, 2)


class StoreQueueTests(TempStoreMixin, unittest.TestCase):
    def test_enqueue_is_idempotent(self):
        store = self.make_store()
        self.assertTrue(store.enqueue_lead("user1", "ctx"))
        self.assertFalse(store.enqueue_lead("user1", "ctx"))
        self.assertEqual(store.pending_count(), 1)

    def test_enqueue_normalizes_username_case(self):
        store = self.make_store()
        self.assertTrue(store.enqueue_lead("Lead_User", "ctx"))
        self.assertFalse(store.enqueue_lead("lead_user", "ctx"))
        self.assertEqual(store.pending_count(), 1)
        claimed = store.claim_next_pending("main", max_attempts=3)
        self.assertEqual(claimed["lead_key"], "lead_user")

    def test_find_lead_is_case_insensitive(self):
        store = self.make_store()
        store.add_contacted("JohnDoe", "main", "sent")

        self.assertEqual(
            store.find_lead("JOHNDOE", None)["lead_key"],
            "johndoe",
        )

    def test_enqueue_skips_already_contacted(self):
        store = self.make_store()
        store.add_contacted("user1", "main", "sent")
        self.assertFalse(store.enqueue_lead("user1", "ctx"))
        self.assertEqual(store.pending_count(), 0)

    def test_claim_release_and_exhaustion(self):
        store = self.make_store()
        store.enqueue_lead("user1", "ctx")

        first = store.claim_next_pending("main", max_attempts=2)
        self.assertEqual(first["status"], "processing")
        self.assertEqual(first["assigned_account"], "main")
        self.assertEqual(first["attempts"], 1)
        self.assertIsNone(store.claim_next_pending("main", max_attempts=2))

        store.release_lead("user1", error="cooldown")
        second = store.claim_next_pending("second", max_attempts=2)
        self.assertEqual(second["attempts"], 2)

        store.release_lead("user1")
        exhausted = store.claim_next_pending("main", max_attempts=2)
        self.assertEqual(exhausted["status"], "failed")
        self.assertEqual(store.pending_count(), 0)

    def test_concurrent_claim_only_assigns_one_account(self):
        store = self.make_store()
        store.enqueue_lead("user1", "ctx")

        results = []

        def claim(account):
            results.append(store.claim_next_pending(account, max_attempts=3))

        import threading

        threads = [threading.Thread(target=claim, args=(name,)) for name in ("main", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertIn(claimed[0]["assigned_account"], {"main", "second"})

    def test_requeue_stuck_after_crash(self):
        store = self.make_store()
        store.enqueue_lead("user1", "ctx")
        store.claim_next_pending("main", max_attempts=5)
        self.assertEqual(store.requeue_stuck(older_than_seconds=-1), 1)
        self.assertEqual(store.pending_count(), 1)

    def test_daily_counter_and_cooldown(self):
        store = self.make_store()
        store.ensure_account("main")
        store.add_contacted("u1", "main", "sent")
        store.add_contacted("u2", "main", "skipped")
        store.add_contacted("u3", "second", "sent")
        self.assertEqual(store.sent_today("main"), 1)

        self.assertEqual(store.get_cooldown_remaining("main"), 0)
        store.activate_cooldown("main", 120, "test")
        self.assertGreater(store.get_cooldown_remaining("main"), 100)
        self.assertEqual(store.get_cooldown_remaining("second"), 0)

    def test_runtime_account_controls_and_dashboard(self):
        store = self.make_store()
        store.ensure_account("main")
        store.set_account_enabled("main", False)
        store.set_daily_limit("main", 7)
        store.add_contacted("cold", "main", "stopped")
        store.add_contacted("warm", "main", "warm_notified")

        self.assertFalse(store.account_enabled("main"))
        self.assertEqual(store.daily_limit("main", 3), 7)
        snapshot = store.dashboard_snapshot()
        self.assertEqual(snapshot["dropped"], 1)
        self.assertEqual(snapshot["warm"], 1)
        self.assertEqual(snapshot["accounts"][0]["enabled"], 0)

    def test_funnel_timeseries_uses_pitch_cohorts_and_account_filter(self):
        store = self.make_store()
        timezone = datetime.timezone(datetime.timedelta(hours=3))
        july_1 = datetime.datetime(2026, 7, 1, 12, tzinfo=timezone).timestamp()
        july_2 = datetime.datetime(2026, 7, 2, 12, tzinfo=timezone).timestamp()

        for lead, account, timestamp in (
            ("read_twice", "main", july_1),
            ("replied_once", "main", july_1),
            ("unread", "personal", july_2),
        ):
            store.add_contacted(lead, account, "sent")
            store.record_message(
                lead, account, "out", "pitch", meta={"kind": "pitch"}
            )
            store._conn.execute(
                "UPDATE leads SET timestamp=? WHERE lead_key=?",
                (timestamp, lead),
            )
        store._conn.commit()

        store.track_pitch("read_twice", 101, 10)
        self.assertFalse(store.mark_read_receipt("main", 101, 9))
        self.assertTrue(store.mark_read_receipt("main", 101, 10))
        store.record_message("read_twice", "main", "in", "Да")
        store.record_message("read_twice", "main", "in", "Давайте")
        store.record_message("replied_once", "main", "in", "Расскажите")
        store._conn.execute(
            """UPDATE leads SET manager_notified_at=1,
                   last_notified_action='handoff_to_manager'
               WHERE lead_key='read_twice'"""
        )
        store._conn.commit()

        funnel = store.funnel_timeseries(
            datetime.date(2026, 7, 1),
            datetime.date(2026, 7, 2),
        )
        self.assertEqual(
            funnel["totals"],
            {
                "sent": 3,
                "read": 2,
                "first_reply": 2,
                "second_reply": 1,
                "recommendation": 1,
                "rates": {
                    "read": 66.7,
                    "first_reply": 66.7,
                    "second_reply": 33.3,
                    "recommendation": 33.3,
                },
            },
        )
        self.assertEqual(funnel["series"][0]["second_reply_cr"], 50.0)
        self.assertEqual(
            store.funnel_timeseries(
                datetime.date(2026, 7, 1),
                datetime.date(2026, 7, 2),
                "personal",
            )["totals"]["sent"],
            1,
        )

    def test_admin_notification_outbox_completes_status_atomically(self):
        store = self.make_store()
        store.add_contacted("warm_lead", "main", "notification_pending")
        store.enqueue_admin_notification(
            "warm_lead",
            "main",
            "MaksIgitov",
            "handoff_to_manager",
            "manager message",
            "warm_notified",
        )

        pending = store.pending_admin_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["recipient"], "MaksIgitov")
        self.assertEqual(pending[0]["action"], "handoff_to_manager")
        self.assertEqual(pending[0]["message"], "manager message")

        store.fail_admin_notification(
            "warm_lead",
            "handoff_to_manager",
            "temporary failure",
        )
        self.assertEqual(store.pending_admin_notifications()[0]["attempts"], 1)
        self.assertTrue(
            store.complete_admin_notification(
                "warm_lead",
                "handoff_to_manager",
            )
        )
        self.assertEqual(store.pending_admin_notifications(), [])
        lead = store.get_lead("warm_lead")
        self.assertEqual(lead["status"], "warm_notified")
        self.assertIsNotNone(lead["manager_notified_at"])
        self.assertEqual(lead["last_notified_action"], "handoff_to_manager")

    def test_handoff_decision_and_outbox_intent_are_atomic(self):
        store = self.make_store()
        store.add_contacted("lead", "main", "sent")
        decision = {
            "stage": "ready_to_test",
            "action": "handoff_to_manager",
            "status": "notification_pending",
        }

        already_notified = store.apply_decision_and_enqueue_notification(
            "lead",
            decision,
            "main",
            "MaksIgitov",
            "warm message",
            "warm_notified",
        )

        self.assertFalse(already_notified)
        lead = store.get_lead("lead")
        self.assertEqual(lead["status"], "notification_pending")
        self.assertEqual(lead["last_action"], "handoff_to_manager")
        pending = store.pending_admin_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "handoff_to_manager")

    def test_orphaned_pending_handoff_is_recovered_on_startup(self):
        store = self.make_store()
        store.add_contacted("lead", "main", "notification_pending")
        store.record_message("lead", "main", "in", "Да, давайте")
        account = config.AccountConfig(
            name="main",
            api_id=1,
            api_hash="hash",
            session="session",
            manager_username="MaksIgitov",
        )

        self.assertEqual(
            recover_orphaned_notifications(
                store,
                [account],
                "fallback_manager",
            ),
            1,
        )
        pending = store.pending_admin_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["recipient"], "MaksIgitov")
        self.assertIn("Да, давайте", pending[0]["message"])
        self.assertEqual(
            recover_orphaned_notifications(
                store,
                [account],
                "fallback_manager",
            ),
            0,
        )

    def test_manual_review_notification_does_not_suppress_handoff(self):
        store = self.make_store()
        store.add_contacted("lead", "main", "sent")
        manual = {"action": "manual_review", "status": "manual_review"}
        handoff = {"action": "handoff_to_manager", "status": "notification_pending"}

        self.assertFalse(store.apply_decision("lead", manual, replied=False))
        store.enqueue_admin_notification(
            "lead",
            "main",
            "MaksIgitov",
            "manual_review",
            "manual message",
            "manual_review",
        )
        store.complete_admin_notification("lead", "manual_review")

        self.assertTrue(store.apply_decision("lead", manual, replied=False))
        self.assertFalse(store.apply_decision("lead", handoff, replied=True))

    def test_outbox_claim_prevents_concurrent_duplicate_delivery(self):
        store = self.make_store()
        store.add_contacted("lead", "main", "notification_pending")
        store.enqueue_admin_notification(
            "lead",
            "main",
            "MaksIgitov",
            "handoff_to_manager",
            "warm message",
            "warm_notified",
        )

        self.assertTrue(
            store.claim_admin_notification("lead", "handoff_to_manager")
        )
        self.assertFalse(
            store.claim_admin_notification("lead", "handoff_to_manager")
        )
        self.assertEqual(store.claim_pending_admin_notifications(), [])

        store.fail_admin_notification(
            "lead",
            "handoff_to_manager",
            "temporary",
        )
        pending = store.pending_admin_notifications()[0]
        self.assertGreater(pending["next_attempt_at"], pending["updated_at"])
        self.assertFalse(
            store.claim_admin_notification("lead", "handoff_to_manager")
        )

    def test_failed_notifications_do_not_starve_new_handoffs(self):
        store = self.make_store()
        for index in range(25):
            lead_key = f"lead_{index:02d}"
            store.add_contacted(lead_key, "main", "notification_pending")
            store.enqueue_admin_notification(
                lead_key,
                "main",
                "MaksIgitov",
                "handoff_to_manager",
                f"message {index}",
                "warm_notified",
            )
            if index < 20:
                store.fail_admin_notification(
                    lead_key,
                    "handoff_to_manager",
                    "poison",
                )

        claimed = store.claim_pending_admin_notifications(limit=20)

        self.assertEqual(
            {item["lead_key"] for item in claimed},
            {f"lead_{index:02d}" for index in range(20, 25)},
        )

    def test_manual_and_handoff_outbox_entries_can_coexist(self):
        store = self.make_store()
        store.add_contacted("lead", "main", "manual_review")
        for action, message, status in (
            ("manual_review", "manual message", "manual_review"),
            ("handoff_to_manager", "warm message", "warm_notified"),
        ):
            store.enqueue_admin_notification(
                "lead",
                "main",
                "MaksIgitov",
                action,
                message,
                status,
            )

        self.assertEqual(
            {item["action"] for item in store.pending_admin_notifications()},
            {"manual_review", "handoff_to_manager"},
        )
        store.complete_admin_notification("lead", "handoff_to_manager")
        store.complete_admin_notification("lead", "manual_review")
        lead = store.get_lead("lead")
        self.assertEqual(lead["status"], "warm_notified")
        self.assertEqual(lead["last_notified_action"], "handoff_to_manager")

    def test_old_single_key_outbox_schema_is_migrated(self):
        import sqlite3

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        connection = sqlite3.connect(tmp.name)
        connection.execute(
            """CREATE TABLE admin_notifications (
                   lead_key TEXT PRIMARY KEY,
                   account TEXT,
                   recipient TEXT,
                   message TEXT NOT NULL,
                   target_status TEXT NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   last_error TEXT,
                   created_at REAL NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        connection.execute(
            """INSERT INTO admin_notifications
                   (lead_key, account, recipient, message, target_status,
                    created_at, updated_at)
               VALUES ('lead', 'main', 'MaksIgitov', 'warm message',
                       'warm_notified', 1, 1)"""
        )
        connection.commit()
        connection.close()

        store = Store(tmp.name)
        self.addCleanup(
            lambda: (
                store.close(),
                os.path.exists(tmp.name) and os.unlink(tmp.name),
            )
        )

        pending = store.pending_admin_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "handoff_to_manager")
        self.assertIn("claimed_at", pending[0])

    def test_old_manual_review_state_is_reopened_and_backfilled(self):
        import sqlite3

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        connection = sqlite3.connect(tmp.name)
        connection.execute(
            """CREATE TABLE leads (
                   lead_key TEXT PRIMARY KEY,
                   peer_id INTEGER,
                   account TEXT,
                   status TEXT NOT NULL,
                   date TEXT,
                   timestamp REAL,
                   reply_count INTEGER NOT NULL DEFAULT 0,
                   last_reply_date TEXT,
                   last_stage TEXT,
                   last_action TEXT,
                   manager_notified_at REAL,
                   stop_reason TEXT
               )"""
        )
        connection.execute(
            """INSERT INTO leads
                   (lead_key, account, status, reply_count, last_action,
                    manager_notified_at)
               VALUES ('lead', 'main', 'manual_review', 999,
                       'manual_review', 1)"""
        )
        connection.commit()
        connection.close()

        store = Store(tmp.name)
        self.addCleanup(
            lambda: (
                store.close(),
                os.path.exists(tmp.name) and os.unlink(tmp.name),
            )
        )

        lead = store.get_lead("lead")
        self.assertEqual(lead["reply_count"], 0)
        self.assertEqual(lead["last_notified_action"], "manual_review")

    def test_legacy_json_migration(self):
        import json

        store = self.make_store()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "contacted": {
                        "old_lead": {
                            "date": "2025-01-01",
                            "timestamp": 1,
                            "reply_count": 2,
                            "status": "in_dialog",
                            "last_stage": "primary_interest",
                        }
                    },
                    "last_reset": "2025-01-01",
                },
                f,
            )
            path = f.name
        self.addCleanup(os.unlink, path)

        self.assertTrue(store.migrate_legacy_json(path, ["main"]))
        self.assertFalse(store.migrate_legacy_json(path, ["main"]))

        lead = store.get_lead("old_lead")
        self.assertEqual(lead["status"], "in_dialog")
        self.assertIsNone(lead["account"])
        self.assertEqual(lead["reply_count"], 2)


class _StubWorker:
    def __init__(self, name, capacity=True):
        self.name = name
        self._capacity = capacity
        self.processed = []

    def has_capacity(self):
        return self._capacity

    async def process_lead(self, item):
        self.processed.append(item["lead_key"])
        return "sent"


class _FakeBrain:
    def build_pitch_message(self, target_user, lead_context):
        return "pitch"


class _FakeClient:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, target_user, text):
        self.sent_messages.append((target_user, text))


class _FakeNotifier:
    async def notify(self, message):
        pass


class _NotifierClient:
    def __init__(self, works):
        self.works = works
        self.sent = []

    async def get_entity(self, chat_id):
        if not self.works:
            raise ValueError("unknown peer")
        return f"entity:{chat_id}"

    async def iter_dialogs(self):
        if False:
            yield None

    async def send_message(self, entity, message):
        self.sent.append((entity, message))


class AdminNotifierTests(unittest.TestCase):
    def test_mtproto_tries_every_connected_account(self):
        first = _NotifierClient(False)
        second = _NotifierClient(True)
        notifier = AdminNotifier(
            "",
            "-100123",
            client_provider=lambda: [first, second],
        )

        channel = asyncio.run(notifier.notify("warm lead"))

        self.assertEqual(channel, "mtproto")
        self.assertEqual(second.sent, [("entity:-100123", "warm lead")])

    def test_notify_raises_when_all_channels_fail(self):
        notifier = AdminNotifier(
            "",
            "-100123",
            client_provider=lambda: [_NotifierClient(False)],
        )

        with self.assertRaises(RuntimeError):
            asyncio.run(notifier.notify("warm lead"))

    def test_direct_manager_is_last_resort(self):
        client = _NotifierClient(False)
        notifier = AdminNotifier(
            "",
            "-100123",
            client_provider=lambda: [client],
        )

        channel = asyncio.run(
            notifier.notify("warm lead", fallback_username="andrew_pontific")
        )

        self.assertEqual(channel, "mtproto:@andrew_pontific")
        self.assertEqual(client.sent, [("@andrew_pontific", "warm lead")])

    def test_transient_bot_api_failure_does_not_disable_route(self):
        client = _NotifierClient(True)
        notifier = AdminNotifier(
            "token",
            "-100123",
            client_provider=lambda: [client],
        )
        notifier._send_via_bot_api = mock.Mock(
            side_effect=RuntimeError("connection timeout")
        )

        channel = asyncio.run(notifier.notify("warm lead"))

        self.assertEqual(channel, "mtproto")
        self.assertFalse(notifier.bot_api_failed)

    def test_permanent_bot_api_failure_disables_route(self):
        client = _NotifierClient(True)
        notifier = AdminNotifier(
            "token",
            "-100123",
            client_provider=lambda: [client],
        )
        notifier._send_via_bot_api = mock.Mock(
            side_effect=RuntimeError("Bad Request: chat not found")
        )

        asyncio.run(notifier.notify("warm lead"))

        self.assertTrue(notifier.bot_api_failed)


class AccountWorkerQueueTests(TempStoreMixin, unittest.TestCase):
    def test_quiet_hours_account_has_no_capacity(self):
        store = self.make_store()
        store.ensure_account("main")
        cfg = config.AccountConfig(name="main", api_id=1, api_hash="hash", session="session")
        worker = AccountWorker(cfg, make_settings(), store, _FakeBrain(), _FakeNotifier())
        worker.is_connected = lambda: True

        with mock.patch("hermes.worker.is_outreach_allowed", return_value=False):
            self.assertFalse(worker.has_capacity())

    def test_disabled_account_has_no_capacity(self):
        store = self.make_store()
        store.ensure_account("main")
        store.set_account_enabled("main", False)
        cfg = config.AccountConfig(name="main", api_id=1, api_hash="hash", session="session")
        worker = AccountWorker(cfg, make_settings(), store, _FakeBrain(), _FakeNotifier())
        worker.is_connected = lambda: True

        self.assertFalse(worker.has_capacity())

    def test_process_lead_skips_existing_lead_without_sending(self):
        store = self.make_store()
        store.add_contacted("lead_x", "other", "sent")
        cfg = config.AccountConfig(name="main", api_id=1, api_hash="hash", session="session")
        worker = AccountWorker(cfg, make_settings(), store, _FakeBrain(), _FakeNotifier())
        worker.client = _FakeClient()

        result = asyncio.run(worker.process_lead({"lead_key": "lead_x", "context": "ctx"}))

        self.assertEqual(result, "duplicate")
        self.assertEqual(worker.client.sent_messages, [])


class DispatcherTests(TempStoreMixin, unittest.TestCase):
    def test_dispatcher_keeps_lead_pending_during_quiet_hours(self):
        store = self.make_store()
        store.ensure_account("a")
        store.enqueue_lead("lead_x", "ctx")
        dispatcher = Dispatcher(store, [_StubWorker("a")], make_settings())

        with mock.patch("hermes.dispatcher.is_outreach_allowed", return_value=False):
            dispatched = asyncio.run(dispatcher._dispatch_once())

        self.assertFalse(dispatched)
        self.assertEqual(store.pending_count(), 1)

    def test_pick_worker_prefers_least_recently_dispatched(self):
        store = self.make_store()
        for name in ("a", "b"):
            store.ensure_account(name)
        store.mark_dispatched("a")

        w_a, w_b = _StubWorker("a"), _StubWorker("b")
        dispatcher = Dispatcher(store, [w_a, w_b], make_settings())
        self.assertIs(dispatcher.pick_worker(), w_b)

    def test_pick_worker_skips_accounts_without_capacity(self):
        store = self.make_store()
        w_a, w_b = _StubWorker("a", capacity=False), _StubWorker("b")
        dispatcher = Dispatcher(store, [w_a, w_b], make_settings())
        self.assertIs(dispatcher.pick_worker(), w_b)

        w_b._capacity = False
        self.assertIsNone(dispatcher.pick_worker())

    def test_dispatch_once_assigns_lead_to_worker(self):
        store = self.make_store()
        store.ensure_account("a")
        store.enqueue_lead("lead_x", "ctx")
        worker = _StubWorker("a")
        dispatcher = Dispatcher(store, [worker], make_settings())

        async def run_case():
            dispatched = await dispatcher._dispatch_once()
            await asyncio.gather(*dispatcher._inflight)
            return dispatched

        self.assertTrue(asyncio.run(run_case()))
        self.assertEqual(worker.processed, ["lead_x"])
        self.assertGreater(store.last_dispatch_at("a"), 0)

    def test_worker_not_double_booked_while_pitch_in_flight(self):
        store = self.make_store()
        store.ensure_account("a")
        store.enqueue_lead("lead_1", "ctx")
        store.enqueue_lead("lead_2", "ctx")
        worker = _StubWorker("a")
        dispatcher = Dispatcher(store, [worker], make_settings())

        async def run_case():
            first = await dispatcher._dispatch_once()
            # pitch task for lead_1 has not started yet; the same account
            # must not be handed lead_2 in the meantime
            second = await dispatcher._dispatch_once()
            await asyncio.gather(*dispatcher._inflight)
            return first, second

        first, second = asyncio.run(run_case())
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(worker.processed, ["lead_1"])
        self.assertEqual(store.pending_count(), 1)


if __name__ == "__main__":
    unittest.main()
