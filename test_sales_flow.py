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


def client_history(message):
    return f"Я (менеджер Пульсар): Добрый день\n\nКлиент (@lead123): {message}"


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
        return bot.generate_conversational_reply(client_history(message))

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


class ExplicitNegativeGuardTests(unittest.TestCase):
    def test_short_refusal_stops(self):
        result = bot.stage_from_explicit_negative(client_history("стоп"))
        self.assertIsNotNone(result)
        self.assertEqual(result["stage"], "not_interested")

    def test_polite_refusal_stops(self):
        result = bot.stage_from_explicit_negative(client_history("Спасибо, не интересно"))
        self.assertIsNotNone(result)
        self.assertEqual(result["stage"], "not_interested")

    def test_negative_substring_inside_word_is_not_refusal(self):
        # "постоплата" contains "стоп" and used to hard-stop a paying lead.
        result = bot.stage_from_explicit_negative(client_history("Интересует постоплата"))
        self.assertIsNone(result)

    def test_question_with_negative_phrase_is_not_refusal(self):
        # A buying question, not a refusal.
        result = bot.stage_from_explicit_negative(
            client_history("мне ничего не надо настраивать?")
        )
        self.assertIsNone(result)

    def test_long_message_is_left_to_llm(self):
        long_message = (
            "сейчас не актуально, но мы планируем расширять отдел продаж в следующем "
            "квартале и я хотел бы вернуться к этому вопросу позже"
        )
        result = bot.stage_from_explicit_negative(client_history(long_message))
        self.assertIsNone(result)


class DailyLimitAndKeyTests(unittest.TestCase):
    def setUp(self):
        self.original_limit = bot.COLD_DM_DAILY_LIMIT
        bot.COLD_DM_DAILY_LIMIT = 2

    def tearDown(self):
        bot.COLD_DM_DAILY_LIMIT = self.original_limit

    def db_with(self, statuses):
        today = str(datetime.date.today())
        return {
            "contacted": {
                f"lead{i}": {"date": today, "status": status}
                for i, status in enumerate(statuses)
            },
            "pending_notifications": [],
            "last_reset": today,
        }

    def test_non_consuming_statuses_do_not_eat_the_limit(self):
        db = self.db_with(["sent", "manual_required", "skipped", "missed_limit"])
        self.assertTrue(bot.can_message_today(db))

    def test_sent_leads_consume_the_limit(self):
        db = self.db_with(["sent", "warm_notified"])
        self.assertFalse(bot.can_message_today(db))

    def test_find_contact_key_is_case_insensitive(self):
        db = {"contacted": {"JohnDoe": {"status": "sent"}}}
        self.assertEqual(bot.find_contact_key(db, "johndoe"), "JohnDoe")
        self.assertEqual(bot.find_contact_key(db, "JOHNDOE", "12345"), "JohnDoe")
        self.assertIsNone(bot.find_contact_key(db, "someoneelse"))

    def test_find_contact_key_matches_sender_id(self):
        db = {"contacted": {"12345": {"status": "sent"}}}
        self.assertEqual(bot.find_contact_key(db, "no_username", "12345"), "12345")


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


class FailingSendClient(FakeClient):
    async def send_message(self, chat_id, text):
        raise RuntimeError("network down")


def handoff_decision():
    return {
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


def manual_review_decision():
    return {
        "stage": "unknown",
        "action": "manual_review",
        "reply_text": "",
        "notify_manager": True,
        "status": "manual_review",
        "reason": "не удалось понять сообщение",
        "confidence": 0.1,
        "requires_action": True,
        "action_reason": "не удалось понять сообщение",
    }


def send_reply_decision():
    return {
        "stage": "primary_interest",
        "action": "send_reply",
        "reply_text": "вот такой ответ",
        "notify_manager": False,
        "status": "in_dialog",
        "reason": "клиенту интересно",
        "confidence": 0.9,
        "requires_action": False,
        "action_reason": "клиенту интересно",
    }


class PrivateReplyPipelineTestCase(unittest.TestCase):
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
        self.notify_result = True

        async def fake_notify(client, message):
            self.notifications.append(message)
            return self.notify_result

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

    def write_db(self, reply_count=0, status="sent"):
        today = str(datetime.date.today())
        with open(bot.DB_FILE, "w") as f:
            json.dump(
                {
                    "contacted": {
                        "lead123": {
                            "date": today,
                            "timestamp": 1,
                            "reply_count": reply_count,
                            "last_reply_date": today,
                            "status": status,
                        }
                    },
                    "last_reset": today,
                },
                f,
            )

    def read_db(self):
        with open(bot.DB_FILE) as f:
            return json.load(f)

    def run_reply(self, client):
        asyncio.run(bot.process_private_reply(client, 12345, "lead123", "lead123"))


class ProcessPrivateReplySmokeTests(PrivateReplyPipelineTestCase):
    def test_process_private_reply_marks_warm_and_notifies_once(self):
        self.write_db()
        bot.generate_conversational_reply = lambda history: handoff_decision()

        async def run_case():
            client = FakeClient()
            await bot.process_private_reply(client, 12345, "lead123", "lead123")
            await bot.process_private_reply(client, 12345, "lead123", "lead123")
            return client

        client = asyncio.run(run_case())

        db = self.read_db()
        lead = db["contacted"]["lead123"]

        self.assertEqual(lead["status"], "warm_notified")
        self.assertEqual(lead["last_stage"], "ready_to_test")
        self.assertEqual(lead["last_action"], "handoff_to_manager")
        self.assertEqual(lead["last_notified_action"], "handoff_to_manager")
        self.assertTrue(lead["manager_notified_at"])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(client.sent_messages), 1)
        self.assertEqual(db["pending_notifications"], [])


class HandoffResilienceTests(PrivateReplyPipelineTestCase):
    def test_manager_notified_even_when_client_reply_fails(self):
        self.write_db()
        bot.generate_conversational_reply = lambda history: handoff_decision()

        client = FailingSendClient()
        self.run_reply(client)

        db = self.read_db()
        lead = db["contacted"]["lead123"]

        self.assertEqual(lead["status"], "warm_notified")
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("НЕ доставлен", self.notifications[0])
        self.assertIn(bot.handoff_message(), self.notifications[0])

    def test_failed_notification_stays_in_outbox(self):
        self.write_db()
        bot.generate_conversational_reply = lambda history: handoff_decision()
        self.notify_result = False

        client = FakeClient()
        self.run_reply(client)

        db = self.read_db()
        # The lead is warm, the notification is queued and will be retried.
        self.assertEqual(db["contacted"]["lead123"]["status"], "warm_notified")
        self.assertEqual(len(db["pending_notifications"]), 1)
        self.assertIn("ТЕПЛЫЙ ЛИД", db["pending_notifications"][0]["text"])

        # Delivery recovers - the outbox drains.
        self.notify_result = True
        asyncio.run(bot.flush_admin_notifications(FakeClient()))
        self.assertEqual(self.read_db()["pending_notifications"], [])

    def test_reply_cap_does_not_block_handoff(self):
        self.write_db(reply_count=bot.REPLY_DAILY_CAP + 5)
        bot.generate_conversational_reply = lambda history: handoff_decision()

        client = FakeClient()
        self.run_reply(client)

        db = self.read_db()
        self.assertEqual(db["contacted"]["lead123"]["status"], "warm_notified")
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(client.sent_messages), 1)

    def test_reply_cap_still_blocks_ordinary_replies(self):
        self.write_db(reply_count=bot.REPLY_DAILY_CAP)
        bot.generate_conversational_reply = lambda history: send_reply_decision()

        client = FakeClient()
        self.run_reply(client)

        db = self.read_db()
        self.assertEqual(db["contacted"]["lead123"]["status"], "sent")
        self.assertEqual(client.sent_messages, [])
        self.assertEqual(self.notifications, [])


class ManualReviewEscalationTests(PrivateReplyPipelineTestCase):
    def test_manual_review_is_not_terminal_and_escalates_to_handoff(self):
        self.write_db()

        client = FakeClient()

        # First message is ambiguous: manager pinged, dialog paused but NOT frozen.
        bot.generate_conversational_reply = lambda history: manual_review_decision()
        self.run_reply(client)

        db = self.read_db()
        self.assertEqual(db["contacted"]["lead123"]["status"], "manual_review")
        self.assertEqual(len(self.notifications), 1)

        # Repeated ambiguity does not spam the manager with duplicates.
        self.run_reply(client)
        self.assertEqual(len(self.notifications), 1)

        # The lead then agrees: the handoff must go through and notify the manager.
        bot.generate_conversational_reply = lambda history: handoff_decision()
        self.run_reply(client)

        db = self.read_db()
        lead = db["contacted"]["lead123"]
        self.assertEqual(lead["status"], "warm_notified")
        self.assertEqual(lead["last_notified_action"], "handoff_to_manager")
        self.assertEqual(len(self.notifications), 2)
        self.assertIn("ТЕПЛЫЙ ЛИД", self.notifications[1])
        self.assertEqual(len(client.sent_messages), 1)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.original_db_file = bot.DB_FILE
        self.original_notify = bot.notify_admin
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        bot.DB_FILE = self.tmp.name
        with open(bot.DB_FILE, "w") as f:
            json.dump({"contacted": {}, "pending_notifications": []}, f)

    def tearDown(self):
        bot.DB_FILE = self.original_db_file
        bot.notify_admin = self.original_notify
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def test_flush_preserves_order_and_stops_on_failure(self):
        delivered = []
        results = iter([True, False])

        async def fake_notify(client, message):
            ok = next(results, False)
            if ok:
                delivered.append(message)
            return ok

        bot.notify_admin = fake_notify

        async def run_case():
            async with bot.db_lock:
                db = bot.load_db()
                bot.queue_admin_notification(db, "first")
                bot.queue_admin_notification(db, "second")
                bot.queue_admin_notification(db, "third")
                bot.save_db(db)
            await bot.flush_admin_notifications(FakeClient())

        asyncio.run(run_case())

        self.assertEqual(delivered, ["first"])
        with open(bot.DB_FILE) as f:
            db = json.load(f)
        self.assertEqual([item["text"] for item in db["pending_notifications"]], ["second", "third"])


if __name__ == "__main__":
    unittest.main()
