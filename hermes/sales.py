import re
import json
import logging

from . import prompts
from .llm import LLMUnavailable

logger = logging.getLogger(__name__)

CONVERSATION_STAGES = {
    "primary_interest",
    "needs_explanation",
    "qualification_needed",
    "objection_without_commitment",
    "ready_to_test",
    "meeting_agreed",
    "contact_or_later",
    "not_interested",
    "negative_or_non_target",
    "unknown",
}

CONVERSATION_ACTIONS = {
    "send_reply",
    "handoff_to_manager",
    "silent_stop",
    "manual_review",
}

HANDOFF_STAGES = {"ready_to_test", "meeting_agreed", "contact_or_later"}
STOP_STAGES = {"not_interested", "negative_or_non_target"}
SEND_REPLY_STAGES = {
    "primary_interest",
    "needs_explanation",
    "qualification_needed",
    "objection_without_commitment",
}
TERMINAL_STATUSES = {"notification_pending", "warm_notified", "stopped"}
LOW_CONFIDENCE_THRESHOLD = 0.55

EXPLICIT_NEGATIVE_RE = re.compile(
    r"(?<![а-яёa-z0-9])("
    r"не\s+(?:интересно|актуально|надо|нужно)"
    r"|не\s+рассматриваем"
    r"|не\s+пишите"
    r"|откажусь"
    r"|отказываюсь"
    r"|удалите"
    r"|стоп"
    r")(?![а-яёa-z0-9])",
    re.IGNORECASE,
)
EXPLICIT_NEGATIVE_MAX_CHARS = 80

QUESTION_BEFORE_HANDOFF_RE = re.compile(
    r"("
    r"\?"
    r"|правильно\s+понял"
    r"|верно\s+понял"
    r"|(?:^|\s)(?:сколько|как|что|где|когда|почему|зачем|какой|какая|какие)\b"
    r")",
    re.IGNORECASE,
)

AFFIRMATIVE_INTEREST_RE = re.compile(
    r"^\s*(?:(?:здравствуйте|добрый\s+(?:день|вечер|утро))[,!.\s]+)?"
    r"(?:да(?:\s*,?\s*интересно)?|давайте|интересно)\s*[.!…]*\s*$",
    re.IGNORECASE,
)

INTEREST_QUESTION_RE = re.compile(
    r"("
    r"интересн\w*(?:\s+ли|\s+посмотреть|\s+узнать|\s+получать)"
    r"|было\s+бы.{0,80}интересн"
    r"|хотите.{0,80}(?:обсудить|посмотреть|подключ)"
    r"|так\w*\s+формат"
    r")",
    re.IGNORECASE | re.DOTALL,
)

MANAGER_CONTACT_PROMISE_RE = re.compile(
    r"("
    r"подключ(?:у|им)\s+@"
    r"|передам\b.{0,80}\bменеджер"
    r"|(?:менеджер|он|мы|я)\b.{0,80}\b(?:свяжется|свяжемся|свяжусь|напишет|напишем|напишу)\b"
    r"|с\s+вами\s+(?:скоро\s+)?свяжется\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def get_last_client_message(history_text):
    matches = re.findall(
        r"Клиент \(@[^)]*\):\s*(.*?)(?=\n\n(?:Я \(менеджер|Клиент \(@)|\Z)",
        history_text,
        re.DOTALL,
    )
    return matches[-1].strip() if matches else ""


def get_last_manager_message(history_text):
    matches = re.findall(
        r"Я \(менеджер [^)]*\):\s*(.*?)(?=\n\n(?:Я \(менеджер|Клиент \(@)|\Z)",
        history_text,
        re.DOTALL,
    )
    return matches[-1].strip() if matches else ""


def extract_json_object(output):
    if not output:
        return None
    output = output.strip()
    if "</think>" in output:
        output = output.split("</think>")[-1].strip()
    output = re.sub(r"^```(?:json)?\s*", "", output)
    output = re.sub(r"\s*```$", "", output)
    json_match = re.search(r"\{.*\}", output, re.DOTALL)
    if json_match:
        output = json_match.group(0)
    try:
        return json.loads(output)
    except Exception:
        return None


def clamp_confidence(value):
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def normalize_stage(stage):
    return stage if stage in CONVERSATION_STAGES else "unknown"


def stage_from_explicit_negative(history_text):
    last_message = get_last_client_message(history_text)
    if not last_message:
        return None
    if len(last_message) > EXPLICIT_NEGATIVE_MAX_CHARS or "?" in last_message:
        return None
    if EXPLICIT_NEGATIVE_RE.search(last_message):
        return {
            "stage": "not_interested",
            "reason": "клиент явно отказался или попросил не писать",
            "confidence": 1.0,
        }
    return None


def defer_handoff_for_client_question(classification, history_text):
    stage = normalize_stage(classification.get("stage"))
    if stage not in HANDOFF_STAGES:
        return classification

    last_message = get_last_client_message(history_text)
    if not last_message or not QUESTION_BEFORE_HANDOFF_RE.search(last_message):
        return classification

    updated = dict(classification)
    updated["stage"] = "needs_explanation"
    updated["reason"] = "клиент готов обсуждать, но в последней реплике задал вопрос; сначала отвечаем на вопрос"
    updated["confidence"] = max(clamp_confidence(updated.get("confidence")), 0.9)
    return updated


def promote_affirmative_interest(classification, history_text):
    stage = normalize_stage(classification.get("stage"))
    if stage in HANDOFF_STAGES or stage in STOP_STAGES:
        return classification

    client_message = get_last_client_message(history_text)
    manager_message = get_last_manager_message(history_text)
    if (
        not AFFIRMATIVE_INTEREST_RE.fullmatch(client_message)
        or "?" not in manager_message
        or not INTEREST_QUESTION_RE.search(manager_message)
    ):
        return classification

    updated = dict(classification)
    updated["stage"] = "ready_to_test"
    updated["reason"] = "клиент подтвердил интерес в ответ на вопрос о формате поиска"
    updated["confidence"] = 1.0
    return updated


def sanitize_outgoing(text):
    return text.replace("—", "-").replace("–", "-")


def decide_action(classification):
    stage = normalize_stage(classification.get("stage"))
    reason = str(classification.get("reason") or "")
    confidence = clamp_confidence(classification.get("confidence"))

    if stage in HANDOFF_STAGES:
        action = "handoff_to_manager"
        status = "warm_notified"
        notify_manager = True
    elif stage in STOP_STAGES:
        action = "silent_stop"
        status = "stopped"
        notify_manager = False
    elif stage == "unknown" or confidence < LOW_CONFIDENCE_THRESHOLD:
        action = "manual_review"
        status = "manual_review"
        notify_manager = True
    else:
        action = "send_reply"
        status = "in_dialog"
        notify_manager = False

    return {
        "stage": stage,
        "action": action,
        "reply_text": "",
        "notify_manager": notify_manager,
        "status": status,
        "reason": reason,
        "confidence": confidence,
        "requires_action": notify_manager,
        "action_reason": reason,
    }


def format_history(messages, sender_username, product_name):
    history_lines = []
    for message in messages:
        if not message.text:
            continue
        speaker = f"Я (менеджер {product_name})" if message.out else f"Клиент (@{sender_username})"
        history_lines.append(f"{speaker}: {message.text}")
    return "\n\n".join(history_lines)


class SalesBrain:
    """All LLM-facing sales logic. Model selection lives in the router, not here."""

    def __init__(self, router, settings):
        self.router = router
        self.settings = settings

    # --- pitch ---------------------------------------------------------

    def handoff_message(self, manager_username=None):
        manager_username = manager_username or self.settings.manager_username
        return (
            f"Отлично, тогда подключу @{manager_username}: он поможет подобрать чаты "
            "и критерии, чтобы тест был полезным. Он свяжется с вами в переписке или поможет "
            "на коротком созвоне."
        )

    def default_pitch(self):
        return (
            f"Добрый день!\n\n"
            f"Развиваю продукт {self.settings.product_name}: он сканирует выбранные Telegram-чаты нейросетью и передает в Telegram только сообщения от людей с подходящим запросом.\n"
            f"Сейчас даем 10 000 проверок сообщений бесплатно.\n\n"
            f"Интересно посмотреть, как это может работать для вашей задачи?"
        )

    def generate_pitch(self, lead_context, lead_key=None):
        try:
            result = self.router.chat(
                "pitch",
                [
                    {"role": "system", "content": prompts.pitch_system_prompt(self.settings.product_name)},
                    {"role": "user", "content": f"Контекст текущего лида:\n{lead_context}"},
                ],
                temperature=0.3,
                max_tokens=1000,
                lead_key=lead_key,
            )
        except LLMUnavailable as e:
            logger.error(f"Failed to generate pitch: {e}")
            return None

        output = result.text
        if not output:
            return None

        if "SKIP" in output.upper():
            if len(output) < 20 or "SKIP" in output.upper()[-50:]:
                return "SKIP"
            if not re.search(r"[А-Яа-я]", output):
                return "SKIP"

        if len(output) > 1000:
            logger.warning(f"Generated pitch is too long ({len(output)} chars). Skipping to avoid spam.")
            return "SKIP"

        if "</think>" in output:
            output = output.split("</think>")[-1].strip()
        elif "<think>" in output:
            return None

        output = re.sub(r"^```[a-zA-Z]*\s*", "", output)
        output = re.sub(r"\s*```\s*$", "", output)
        return sanitize_outgoing(output).strip()

    def build_pitch_message(self, target_user, lead_context):
        pitch_message = self.generate_pitch(lead_context, lead_key=target_user)

        if pitch_message and pitch_message.strip().upper() == "SKIP":
            logger.info(f"AI decided to SKIP lead {target_user}.")
            return "SKIP"

        if not pitch_message:
            logger.warning("Could not generate pitch message. Falling back to default.")
            pitch_message = self.default_pitch()

        return pitch_message

    # --- conversation ----------------------------------------------------

    def _query_stage_classifier(self, history_text, lead_key=None):
        try:
            result = self.router.chat(
                "classify",
                [
                    {"role": "system", "content": prompts.classify_system_prompt(self.settings.product_name)},
                    {"role": "user", "content": f"ИСТОРИЯ ДИАЛОГА:\n{history_text}"},
                ],
                temperature=0,
                max_tokens=500,
                lead_key=lead_key,
            )
        except LLMUnavailable as e:
            logger.error(f"Failed to classify conversation stage: {e}")
            return {"stage": "unknown", "reason": "ошибка классификатора", "confidence": 0.0}

        parsed = extract_json_object(result.text)
        if not parsed:
            return {"stage": "unknown", "reason": "не удалось разобрать JSON классификатора", "confidence": 0.0}
        return {
            "stage": normalize_stage(parsed.get("stage")),
            "reason": str(parsed.get("reason") or "стадия определена классификатором")[:300],
            "confidence": clamp_confidence(parsed.get("confidence")),
            "model": f"{result.provider}:{result.model}",
        }

    def classify_conversation_stage(self, history_text, lead_key=None):
        explicit_negative = stage_from_explicit_negative(history_text)
        if explicit_negative:
            return explicit_negative

        first = self._query_stage_classifier(history_text, lead_key=lead_key)
        if (
            first["stage"] != "unknown"
            and first["confidence"] >= LOW_CONFIDENCE_THRESHOLD
        ):
            return first

        retry = self._query_stage_classifier(history_text, lead_key=lead_key)
        return max(
            (first, retry),
            key=lambda item: (
                item["stage"] != "unknown",
                item["confidence"],
            ),
        )

    def fallback_reply_for_stage(self, stage, manager_username=None):
        manager_username = manager_username or self.settings.manager_username
        return (
            f"{self.settings.product_name} сканирует выбранные чаты в телеграм, отбирает сообщения ваших потенциальных клиентов "
            f"и присылает Вам. Подробнее можете ознакомиться на сайте {self.settings.product_url}. А сами чаты Вам поможет "
            f"подобрать @{manager_username}, наш менеджер по работе с клиентами. "
            "Было бы Вам интересно получать клиентов в таком формате?"
        )

    def render_stage_reply(self, decision, history_text, lead_key=None, manager_username=None):
        stage = decision["stage"]
        manager_username = manager_username or self.settings.manager_username
        try:
            result = self.router.chat(
                "reply",
                [
                    {
                        "role": "system",
                        "content": prompts.reply_system_prompt(
                            self.settings.product_name,
                            self.settings.product_url,
                            stage,
                            manager_username,
                        ),
                    },
                    {"role": "user", "content": f"ИСТОРИЯ ДИАЛОГА:\n{history_text}"},
                ],
                temperature=0.2,
                max_tokens=500,
                lead_key=lead_key,
            )
        except LLMUnavailable as e:
            logger.error(f"Failed to render stage reply: {e}")
            return self.fallback_reply_for_stage(stage, manager_username=manager_username)

        output = result.text
        if not output:
            return self.fallback_reply_for_stage(stage, manager_username=manager_username)
        if "</think>" in output:
            output = output.split("</think>")[-1].strip()
        output = re.sub(r"^```[a-zA-Z]*\s*", "", output)
        output = re.sub(r"\s*```\s*$", "", output)
        output = output.strip().strip('"')
        output = sanitize_outgoing(output)
        if len(output) > 600:
            logger.warning(f"Generated stage reply is too long ({len(output)} chars). Using fallback.")
            return self.fallback_reply_for_stage(stage, manager_username=manager_username)
        return output or self.fallback_reply_for_stage(stage, manager_username=manager_username)

    def build_reply_for_decision(self, decision, history_text, lead_key=None, manager_username=None):
        manager_username = manager_username or self.settings.manager_username
        action = decision.get("action")
        if action == "handoff_to_manager":
            decision["reply_text"] = self.handoff_message(manager_username=manager_username)
        elif action in {"silent_stop", "manual_review"}:
            decision["reply_text"] = ""
        elif action == "send_reply":
            decision["reply_text"] = self.render_stage_reply(
                decision,
                history_text,
                lead_key=lead_key,
                manager_username=manager_username,
            )
            if MANAGER_CONTACT_PROMISE_RE.search(decision["reply_text"]):
                model = decision.get("model")
                reply_text = decision["reply_text"]
                decision.update(
                    decide_action(
                        {
                            "stage": "ready_to_test",
                            "reason": "ответ обещает клиенту связь с менеджером",
                            "confidence": max(decision.get("confidence", 0), 0.9),
                        }
                    )
                )
                decision["reply_text"] = reply_text
                if model:
                    decision["model"] = model
        else:
            decision["action"] = "manual_review"
            decision["status"] = "manual_review"
            decision["notify_manager"] = True
            decision["requires_action"] = True
            decision["reply_text"] = ""

        if decision.get("reply_text"):
            decision["reply_text"] = sanitize_outgoing(decision["reply_text"])
        return decision

    def generate_conversational_reply(self, history_text, lead_key=None, manager_username=None):
        classification = self.classify_conversation_stage(history_text, lead_key=lead_key)
        classification = promote_affirmative_interest(classification, history_text)
        classification = defer_handoff_for_client_question(classification, history_text)
        decision = decide_action(classification)
        if classification.get("model"):
            decision["model"] = classification["model"]
        return self.build_reply_for_decision(
            decision,
            history_text,
            lead_key=lead_key,
            manager_username=manager_username,
        )


def manual_message_notice(target_user, message_text):
    if not message_text:
        return f"Напишите вручную @{target_user}."
    return f"Напишите вручную @{target_user}: \"{message_text}\""


def manager_notification_text(
    sender_username,
    decision,
    history_text,
    account=None,
    account_username=None,
    account_display_name=None,
    manager_username=None,
    delivery_note="",
):
    action = decision.get("action", "manual_review")
    title = "🔥 ТЕПЛЫЙ ЛИД" if action == "handoff_to_manager" else "⚠️ РУЧНАЯ ПРОВЕРКА"
    last_message = get_last_client_message(history_text) or "<не удалось выделить последнюю реплику>"
    confidence = clamp_confidence(decision.get("confidence"))
    if account_username:
        username_label = f"@{account_username.lstrip('@')}"
        account_label = (
            f"{account_display_name} ({username_label})"
            if account_display_name
            else username_label
        )
    elif account_display_name:
        account_label = account_display_name
    else:
        account_label = account or ""
    account_line = (
        f"Telegram-профиль отправителя: {account_label}\n"
        if account_label
        else ""
    )
    manager_line = (
        f"Ответственный менеджер: @{manager_username.lstrip('@')}\n"
        if manager_username
        else ""
    )
    model_line = f"Модель: {decision['model']}\n" if decision.get("model") else ""
    note_block = f"{delivery_note}\n\n" if delivery_note else ""
    return (
        f"{title} @{sender_username}\n\n"
        f"{manager_line}"
        f"{account_line}"
        f"Stage: {decision.get('stage', 'unknown')}\n"
        f"Action: {action}\n"
        f"Confidence: {confidence:.2f}\n"
        f"{model_line}"
        f"Причина: {decision.get('reason') or decision.get('action_reason') or 'нет причины'}\n\n"
        f"{note_block}"
        f"Последняя реплика клиента:\n{last_message}\n\n"
        f"История переписки:\n\n{history_text}"
    )


def extract_target_username(text):
    labeled_match = re.search(
        r"^\s*(?:юзернейм|username)\s*:\s*@([A-Za-z0-9_]{5,32})\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if labeled_match:
        return labeled_match.group(1)

    separator = "────────────────────"
    parts = text.split(separator)
    for block in parts[1::2]:
        username_match = re.search(r"@([A-Za-z0-9_]{5,32})\)", block)
        if username_match:
            return username_match.group(1)

    for line in text.splitlines()[:20]:
        if "👤" not in line:
            continue
        username_match = re.search(r"@([A-Za-z0-9_]{5,32})\)", line)
        if username_match:
            return username_match.group(1)
    return None


def extract_lead_context(text):
    context_match = re.search(r"📄\s*Оригинал\n+(.*)", text, re.DOTALL)
    if context_match:
        return context_match.group(1).strip()
    labeled_match = re.search(
        r"^\s*(?:сообщение|message)\s*:\s*(.*)\Z",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if labeled_match:
        return labeled_match.group(1).strip()
    return text
