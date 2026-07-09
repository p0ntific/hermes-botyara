import asyncio
import logging

import requests

logger = logging.getLogger(__name__)


class AdminNotifier:
    """Admin notifications: Bot API first, MTProto through any healthy account as fallback."""

    def __init__(self, bot_token, bot_chat_id, proxy_url="", client_provider=None):
        self.bot_token = bot_token
        self.bot_chat_id = bot_chat_id
        self.proxy_url = proxy_url
        self.client_provider = client_provider or (lambda: None)
        self.bot_api_failed = False
        self._admin_entity = None

    async def notify(self, message):
        if not self.bot_chat_id:
            return

        if self.bot_token and not self.bot_api_failed:
            try:
                await asyncio.to_thread(self._send_via_bot_api, message)
                return
            except Exception as e:
                self.bot_api_failed = True
                safe_error = str(e).replace(self.bot_token, "<token>") if self.bot_token else str(e)
                logger.error(
                    "Failed to send admin notification via Bot API; "
                    f"disabling Bot API notifications until restart: {safe_error}"
                )

        try:
            await self._send_via_mtproto(message)
        except Exception as e:
            logger.error(f"Failed to send admin notification via MTProto: {e}")

    def _send_via_bot_api(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        payload = {"chat_id": self.bot_chat_id, "text": message}
        response = requests.post(url, json=payload, proxies=proxies, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data)

    async def _send_via_mtproto(self, message):
        client = self.client_provider()
        if client is None:
            raise RuntimeError("No healthy Telegram client available for admin notification")

        if self._admin_entity is None:
            self._admin_entity = await self._resolve_admin_entity(client)
        if self._admin_entity is None:
            raise RuntimeError(f"Could not resolve admin chat entity for BOT_CHAT_ID={self.bot_chat_id}")

        await client.send_message(self._admin_entity, message)

    async def _resolve_admin_entity(self, client):
        target_ids = set()
        chat_id_text = str(self.bot_chat_id)
        try:
            target_ids.add(abs(int(chat_id_text)))
        except ValueError:
            pass
        if chat_id_text.startswith("-100"):
            target_ids.add(int(chat_id_text[4:]))
        elif chat_id_text.startswith("-"):
            target_ids.add(int(chat_id_text[1:]))

        try:
            return await client.get_entity(int(self.bot_chat_id))
        except Exception:
            async for dialog in client.iter_dialogs():
                raw_id = getattr(dialog.entity, "id", None)
                dialog_ids = {abs(int(dialog.id))}
                if raw_id is not None:
                    dialog_ids.add(abs(int(raw_id)))
                if target_ids.intersection(dialog_ids):
                    return dialog.entity
        return None
