import asyncio
import logging

import requests

logger = logging.getLogger(__name__)


class AdminNotifier:
    """Admin notifications: Bot API first, then every healthy MTProto account."""

    def __init__(self, bot_token, bot_chat_id, proxy_url="", client_provider=None):
        self.bot_token = bot_token
        self.bot_chat_id = bot_chat_id
        self.proxy_url = proxy_url
        self.client_provider = client_provider or (lambda: None)
        self.bot_api_failed = False
        self.mtproto_admin_failed = False

    async def notify(self, message, fallback_username=None, preferred_client=None):
        if not self.bot_chat_id:
            raise RuntimeError("BOT_CHAT_ID is not configured")

        errors = []
        if self.bot_token and not self.bot_api_failed:
            try:
                await asyncio.to_thread(self._send_via_bot_api, message)
                return "bot_api"
            except Exception as e:
                self.bot_api_failed = True
                safe_error = str(e).replace(self.bot_token, "<token>") if self.bot_token else str(e)
                errors.append(f"Bot API: {safe_error}")
                logger.error(
                    "Failed to send admin notification via Bot API; "
                    f"disabling Bot API notifications until restart: {safe_error}"
                )

        if not self.mtproto_admin_failed:
            try:
                await self._send_via_mtproto(message, preferred_client=preferred_client)
                return "mtproto"
            except Exception as e:
                self.mtproto_admin_failed = True
                logger.error(
                    "Failed to send admin notification via MTProto admin chat; "
                    f"disabling that route until restart: {e}"
                )
                errors.append(f"MTProto: {e}")

        if fallback_username:
            try:
                await self._send_direct_via_mtproto(
                    fallback_username,
                    message,
                    preferred_client=preferred_client,
                )
                return f"mtproto:@{fallback_username.lstrip('@')}"
            except Exception as e:
                logger.error(
                    f"Failed to send admin notification directly to "
                    f"@{fallback_username.lstrip('@')}: {e}"
                )
                errors.append(f"direct MTProto: {e}")

        raise RuntimeError("; ".join(errors))

    def _send_via_bot_api(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        payload = {"chat_id": self.bot_chat_id, "text": message}
        response = requests.post(url, json=payload, proxies=proxies, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data)

    async def _send_via_mtproto(self, message, preferred_client=None):
        clients = self._connected_clients(preferred_client)
        if not clients:
            raise RuntimeError("No healthy Telegram client available for admin notification")

        errors = []
        for client in clients:
            try:
                entity = await self._resolve_admin_entity(client)
                if entity is None:
                    raise RuntimeError(
                        f"Could not resolve admin chat entity for BOT_CHAT_ID={self.bot_chat_id}"
                    )
                await client.send_message(entity, message)
                return
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
        raise RuntimeError("all connected accounts failed: " + " | ".join(errors))

    async def _send_direct_via_mtproto(self, username, message, preferred_client=None):
        clients = self._connected_clients(preferred_client)
        if not clients:
            raise RuntimeError("No healthy Telegram client available for direct notification")

        target = username if str(username).startswith("@") else f"@{username}"
        errors = []
        for client in clients:
            try:
                await client.send_message(target, message)
                return
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
        raise RuntimeError("all connected accounts failed: " + " | ".join(errors))

    def _connected_clients(self, preferred_client=None):
        clients = self.client_provider()
        if clients is None:
            clients = []
        elif not isinstance(clients, (list, tuple)):
            clients = [clients]
        clients = [client for client in clients if client is not None]
        if preferred_client in clients:
            clients.remove(preferred_client)
            clients.insert(0, preferred_client)
        return clients

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
