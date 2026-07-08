import asyncio
import datetime
import json
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

import tg_lead_skill as bot


class SalesFlowDecisionTests(unittest.TestCase):
    def setUp(self):
        self.original_classifier = bot.classify_conversation_stage
        self.original_renderer = bot.render_stage_reply
        bot.render_stage_reply = lambda decision, history: f"reply for {decision['stage']}"

    def tearDown(self):
        bot.classify_conversation_stage = self.original_classifier
        bot.render_stage_reply = self.original_renderer

    def set_stage(self, stage, confidence=0.95, reason="test reason"):
        bot.classify_conversation_stage = lambda history: {
            "stage": stage,
            "confidence": confidence,
            "reason": reason,
        }

    def decide_for_message(self, message):
        history = f"Я (менеджер Пульсар): Добрый день\n\nКлиент (@lead123): {message}"
        return bot.generate_conversational_reply(history)

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
        self.assertEqual(decision["reply_text"], bot.handoff_message())

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

    async def send_read_acknowledge(self, chat_id):
        return None

    def action(self, chat_id, action_name):
        return FakeAction()

    async def get_messages(self, chat_id, limit=20):
        return list(self.history[:limit])

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))


class ProcessPrivateReplySmokeTests(unittest.TestCase):
    def setUp(self):
        self.original_db_file = bot.DB_FILE
        self.original_debounce = bot.INCOMING_REPLY_DEBOUNCE_SECONDS
        self.original_reply = bot.generate_conversational_reply
        self.original_notify = bot.notify_admin
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        bot.DB_FILE = self.tmp.name
        bot.INCOMING_REPLY_DEBOUNCE_SECONDS = 0
        self.notifications = []

        today = str(datetime.date.today())
        with open(bot.DB_FILE, "w") as f:
            json.dump(
                {
                    "contacted": {
                        "lead123": {
                            "date": today,
                            "timestamp": 1,
                            "reply_count": 0,
                            "last_reply_date": today,
                            "status": "sent",
                        }
                    },
                    "last_reset": today,
                },
                f,
            )

        bot.generate_conversational_reply = lambda history: {
            "stage": "ready_to_test",
            "action": "handoff_to_manager",
            "reply_text": bot.handoff_message(),
            "notify_manager": True,
            "status": "warm_notified",
            "reason": "клиент готов тестировать",
            "confidence": 0.98,
            "requires_action": True,
            "action_reason": "клиент готов тестировать",
        }

        async def fake_notify(client, message):
            self.notifications.append(message)

        bot.notify_admin = fake_notify

    def tearDown(self):
        bot.DB_FILE = self.original_db_file
        bot.INCOMING_REPLY_DEBOUNCE_SECONDS = self.original_debounce
        bot.generate_conversational_reply = self.original_reply
        bot.notify_admin = self.original_notify
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def test_process_private_reply_marks_warm_and_notifies_once(self):
        async def run_case():
            client = FakeClient()
            await bot.process_private_reply(client, 12345, "lead123", "lead123")
            await bot.process_private_reply(client, 12345, "lead123", "lead123")
            return client

        client = asyncio.run(run_case())

        with open(bot.DB_FILE) as f:
            db = json.load(f)
        lead = db["contacted"]["lead123"]

        self.assertEqual(lead["status"], "warm_notified")
        self.assertEqual(lead["last_stage"], "ready_to_test")
        self.assertEqual(lead["last_action"], "handoff_to_manager")
        self.assertEqual(lead["reply_count"], 999)
        self.assertTrue(lead["manager_notified_at"])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(client.sent_messages), 1)


if __name__ == "__main__":
    unittest.main()
