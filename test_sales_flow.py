import asyncio
import os
import sys
import tempfile
import types
import unittest


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
    telethon.errors = types.SimpleNamespace(FloodWaitError=Exception)
    sys.modules["telethon"] = telethon

if "telethon.sessions" not in sys.modules:
    sys.modules["telethon.sessions"] = types.SimpleNamespace(StringSession=object)

from hermes import sales
from hermes import worker as worker_module
from hermes.config import AccountConfig, Settings
from hermes.sales import SalesBrain
from hermes.store import Store
from hermes.worker import AccountWorker


def make_settings(**overrides):
    values = dict(
        target_chat=-100123,
        bot_token="",
        bot_chat_id="",
        proxy_url="",
        manager_username="MaksIgitov",
        product_url="https://pulsar-tg.ru/",
        product_name="Пульсар",
        cold_dm_daily_limit=5,
        send_delay_min_seconds=0,
        send_delay_max_seconds=0,
        generic_limit_cooldown_seconds=18000,
        flood_wait_extra_seconds=300,
        incoming_reply_debounce_seconds=0,
        max_pitch_attempts=5,
        db_path=":memory:",
        legacy_json_path="",
        accounts_file="",
    )
    values.update(overrides)
    return Settings(**values)


class SalesFlowDecisionTests(unittest.TestCase):
    def setUp(self):
        self.brain = SalesBrain(router=None, settings=make_settings())
        self.brain.render_stage_reply = lambda decision, history, lead_key=None, manager_username=None: (
            f"reply for {decision['stage']}"
        )

    def set_stage(self, stage, confidence=0.95, reason="test reason"):
        self.brain.classify_conversation_stage = lambda history, lead_key=None: {
            "stage": stage,
            "confidence": confidence,
            "reason": reason,
        }

    def decide_for_message(self, message):
        history = f"Я (менеджер Пульсар): Добрый день\n\nКлиент (@lead123): {message}"
        return self.brain.generate_conversational_reply(history)

    def test_primary_interest_sends_reply_without_manager(self):
        self.set_stage("primary_interest")

        decision = self.decide_for_message("интересно, расскажите")

        self.assertEqual(decision["stage"], "primary_interest")
        self.assertEqual(decision["action"], "send_reply")
        self.assertEqual(decision["status"], "in_dialog")
        self.assertFalse(decision["notify_manager"])
        self.assertTrue(decision["reply_text"])

    def test_ready_to_test_handoffs_to_manager(self):
        self.set_stage("ready_to_test")

        decision = self.decide_for_message("с радостью попробую, но боюсь")

        self.assertEqual(decision["action"], "handoff_to_manager")
        self.assertEqual(decision["status"], "warm_notified")
        self.assertTrue(decision["notify_manager"])
        self.assertIn("Он свяжется с вами", decision["reply_text"])
        self.assertEqual(decision["reply_text"], self.brain.handoff_message())

    def test_affirmative_answer_to_interest_question_handoffs(self):
        self.set_stage("primary_interest")

        for message in ("Да", "Да, интересно", "Давайте", "Добрый день, да"):
            with self.subTest(message=message):
                history = (
                    "Я (менеджер Пульсар): Было бы Вам интересно получать клиентов "
                    f"в таком формате?\n\nКлиент (@lead123): {message}"
                )
                decision = self.brain.generate_conversational_reply(history)

                self.assertEqual(decision["stage"], "ready_to_test")
                self.assertEqual(decision["action"], "handoff_to_manager")
                self.assertTrue(decision["notify_manager"])

    def test_affirmative_answer_to_unrelated_question_keeps_dialog(self):
        self.set_stage("primary_interest")
        history = "Я (менеджер Пульсар): Ссылка открылась?\n\nКлиент (@lead123): Да"

        decision = self.brain.generate_conversational_reply(history)

        self.assertEqual(decision["action"], "send_reply")
        self.assertFalse(decision["notify_manager"])

    def test_manager_contact_promise_forces_handoff(self):
        self.set_stage("primary_interest")
        self.brain.render_stage_reply = lambda *args, **kwargs: (
            "Передам информацию менеджеру. Он свяжется с вами."
        )

        decision = self.decide_for_message("интересно, расскажите")

        self.assertEqual(decision["stage"], "ready_to_test")
        self.assertEqual(decision["action"], "handoff_to_manager")
        self.assertEqual(decision["status"], "warm_notified")
        self.assertTrue(decision["notify_manager"])
        self.assertIn("Он свяжется с вами", decision["reply_text"])

    def test_ready_to_test_with_client_question_answers_before_handoff(self):
        self.set_stage("ready_to_test")

        decision = self.decide_for_message("Да давайте. 10 000 сообщений бесплатно, я правильно понял?")

        self.assertEqual(decision["stage"], "needs_explanation")
        self.assertEqual(decision["action"], "send_reply")
        self.assertEqual(decision["status"], "in_dialog")
        self.assertFalse(decision["notify_manager"])
        self.assertEqual(decision["reply_text"], "reply for needs_explanation")

    def test_handoff_can_target_account_specific_manager(self):
        self.set_stage("ready_to_test")
        history = "Я (менеджер Пульсар): Добрый день\n\nКлиент (@lead123): готов тестировать"

        decision = self.brain.generate_conversational_reply(
            history,
            manager_username="andrew_pontific",
        )

        self.assertIn("@andrew_pontific", decision["reply_text"])
        self.assertNotIn("@MaksIgitov", decision["reply_text"])

    def test_objection_without_commitment_keeps_dialog(self):
        self.set_stage("objection_without_commitment")

        decision = self.decide_for_message("боюсь, пока не готов")

        self.assertEqual(decision["action"], "send_reply")
        self.assertEqual(decision["status"], "in_dialog")
        self.assertFalse(decision["notify_manager"])
        self.assertTrue(decision["reply_text"])

    def test_meeting_agreed_handoffs_to_manager(self):
        self.set_stage("meeting_agreed")

        decision = self.decide_for_message("давайте созвонимся")

        self.assertEqual(decision["action"], "handoff_to_manager")
        self.assertEqual(decision["status"], "warm_notified")
        self.assertTrue(decision["notify_manager"])

    def test_contact_or_later_handoffs_to_manager(self):
        self.set_stage("contact_or_later")

        decision = self.decide_for_message("напишите завтра моему партнеру @partner")

        self.assertEqual(decision["action"], "handoff_to_manager")
        self.assertEqual(decision["status"], "warm_notified")
        self.assertTrue(decision["notify_manager"])

    def test_not_interested_stops_silently(self):
        self.set_stage("not_interested")

        decision = self.decide_for_message("не интересно")

        self.assertEqual(decision["action"], "silent_stop")
        self.assertEqual(decision["status"], "stopped")
        self.assertFalse(decision["notify_manager"])
        self.assertEqual(decision["reply_text"], "")

    def test_negative_or_non_target_stops_silently(self):
        self.set_stage("negative_or_non_target")

        decision = self.decide_for_message("купите лучше мои услуги")

        self.assertEqual(decision["action"], "silent_stop")
        self.assertEqual(decision["status"], "stopped")
        self.assertEqual(decision["reply_text"], "")

    def test_unknown_goes_to_manual_review_without_reply(self):
        self.set_stage("unknown", confidence=0.1)

        decision = self.decide_for_message("???")

        self.assertEqual(decision["action"], "manual_review")
        self.assertEqual(decision["status"], "manual_review")
        self.assertTrue(decision["notify_manager"])
        self.assertEqual(decision["reply_text"], "")

    def test_low_confidence_goes_to_manual_review(self):
        self.set_stage("primary_interest", confidence=0.3)

        decision = self.decide_for_message("может быть потом")

        self.assertEqual(decision["action"], "manual_review")
        self.assertEqual(decision["status"], "manual_review")
        self.assertTrue(decision["notify_manager"])
        self.assertEqual(decision["reply_text"], "")

    def test_explicit_negative_short_circuits_llm(self):
        brain = SalesBrain(router=None, settings=make_settings())
        history = "Я (менеджер Пульсар): Добрый день\n\nКлиент (@lead123): не интересно, не пишите"

        classification = brain.classify_conversation_stage(history)

        self.assertEqual(classification["stage"], "not_interested")
        self.assertEqual(classification["confidence"], 1.0)

    def test_fallback_followup_asks_only_about_search_format(self):
        reply = self.brain.fallback_reply_for_stage("primary_interest")

        self.assertIn("сканирует выбранные чаты в телеграм", reply)
        self.assertIn("сообщения ваших потенциальных клиентов", reply)
        self.assertIn("https://pulsar-tg.ru/", reply)
        self.assertIn("@MaksIgitov", reply)
        self.assertIn("Было бы Вам интересно получать клиентов в таком формате?", reply)
        self.assertNotIn("каких клиентов", reply)
        self.assertNotIn("какие запросы", reply)
        self.assertNotIn("каких чатах", reply)

    def test_fallback_can_target_account_specific_manager(self):
        reply = self.brain.fallback_reply_for_stage(
            "primary_interest",
            manager_username="andrew_pontific",
        )

        self.assertIn("@andrew_pontific", reply)
        self.assertNotIn("@MaksIgitov", reply)


class ExtractionTests(unittest.TestCase):
    def test_extract_target_username_from_header(self):
        text = "🔥 Новый лид\n👤 Иван (@ivan_lead)\nостальное"
        self.assertEqual(sales.extract_target_username(text), "ivan_lead")

    def test_extract_lead_context(self):
        text = "заголовок\n📄 Оригинал\n\nищу инструмент для лидов"
        self.assertEqual(sales.extract_lead_context(text), "ищу инструмент для лидов")


class FakeAction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMessage:
    def __init__(self, text, out=False):
        self.text = text
        self.out = out


class FakeClient:
    def __init__(self):
        self.sent_messages = []
        self.history = [
            FakeMessage("с радостью попробую, но боюсь", out=False),
            FakeMessage("Интересно посмотреть, как это может работать?", out=True),
        ]

    def is_connected(self):
        return True

    async def send_read_acknowledge(self, chat_id):
        return None

    def action(self, chat_id, action_name):
        return FakeAction()

    async def get_messages(self, chat_id, limit=20):
        return list(self.history[:limit])

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    async def notify(self, message, fallback_username=None, preferred_client=None):
        self.notifications.append(message)
        return "fake"


class FakeSender:
    def __init__(self, id, username):
        self.id = id
        self.username = username


class FakeEvent:
    def __init__(self, sender, chat_id=1, text="привет"):
        self.is_private = True
        self.chat_id = chat_id
        self.raw_text = text
        self._sender = sender

    async def get_sender(self):
        return self._sender


class ProcessPrivateReplySmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.settings = make_settings(db_path=self.tmp.name)
        self.store = Store(self.tmp.name)
        self.store.add_contacted("lead123", "main", "sent")

        self.brain = SalesBrain(router=None, settings=self.settings)
        handoff = self.brain.handoff_message()
        self.brain.generate_conversational_reply = lambda history, lead_key=None, manager_username=None: {
            "stage": "ready_to_test",
            "action": "handoff_to_manager",
            "reply_text": handoff,
            "notify_manager": True,
            "status": "warm_notified",
            "reason": "клиент готов тестировать",
            "confidence": 0.98,
            "requires_action": True,
            "action_reason": "клиент готов тестировать",
        }

        self.notifier = FakeNotifier()
        cfg = AccountConfig(name="main", api_id=1, api_hash="x", session="s")
        self.worker = AccountWorker(cfg, self.settings, self.store, self.brain, self.notifier)
        self.worker.client = FakeClient()
        self.worker.healthy = True

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def test_process_private_reply_marks_warm_and_notifies_once(self):
        async def run_case():
            await self.worker.process_private_reply(12345, "lead123", "lead123")
            await self.worker.process_private_reply(12345, "lead123", "lead123")

        asyncio.run(run_case())

        lead = self.store.get_lead("lead123")
        self.assertEqual(lead["status"], "warm_notified")
        self.assertEqual(lead["last_stage"], "ready_to_test")
        self.assertEqual(lead["last_action"], "handoff_to_manager")
        self.assertEqual(lead["reply_count"], 999)
        self.assertTrue(lead["manager_notified_at"])
        self.assertEqual(len(self.notifier.notifications), 1)
        self.assertEqual(len(self.worker.client.sent_messages), 1)
        self.assertIn("Аккаунт: main", self.notifier.notifications[0])

        transcript = self.store.get_transcript("lead123")
        directions = [t["direction"] for t in transcript]
        self.assertIn("event", directions)
        self.assertIn("out", directions)

    def test_pm_handler_ignores_lead_of_another_account(self):
        self.store.add_contacted("other_lead", "second", "sent")

        async def run_case():
            await self.worker._pm_handler(FakeEvent(FakeSender(555, "other_lead")))
            await asyncio.sleep(0)

        asyncio.run(run_case())
        self.assertEqual(self.worker.pending_reply_tasks, {})
        self.assertEqual(len(self.worker.client.sent_messages), 0)

    def test_pm_handler_claims_legacy_lead_without_account(self):
        self.store.add_contacted("legacy_lead", None, "sent")

        async def run_case():
            await self.worker._pm_handler(FakeEvent(FakeSender(777, "legacy_lead")))
            task = self.worker.pending_reply_tasks.get("legacy_lead")
            self.assertIsNotNone(task)
            await task

        asyncio.run(run_case())
        lead = self.store.get_lead("legacy_lead")
        self.assertEqual(lead["account"], "main")
        self.assertEqual(lead["peer_id"], 777)


class AccountConnectionTests(unittest.TestCase):
    def test_build_client_normalizes_session_port_to_443(self):
        class FakeSession:
            dc_id = 2
            server_address = "149.154.167.51"
            port = 80

            def set_dc(self, dc_id, server_address, port):
                self.port = port

        session = FakeSession()
        original_session = worker_module.StringSession
        original_client = worker_module.TelegramClient
        worker_module.StringSession = lambda value: session
        worker_module.TelegramClient = lambda session, *args, **kwargs: session
        try:
            cfg = AccountConfig(name="personal", api_id=1, api_hash="x", session="s")
            worker = AccountWorker(cfg, make_settings(), None, None, None)

            client = worker.build_client()

            self.assertEqual(client.port, 443)
        finally:
            worker_module.StringSession = original_session
            worker_module.TelegramClient = original_client


if __name__ == "__main__":
    unittest.main()
