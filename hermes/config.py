import os
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

LLM_TASKS = ("pitch", "classify", "reply")

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value):
    if isinstance(value, str):
        return _ENV_REF_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    return value


def _int_env(name, default):
    raw = os.getenv(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def parse_proxy(proxy_url):
    """Expected format: socks5h://127.0.0.1:9051 -> telethon proxy dict."""
    if not proxy_url:
        return None
    try:
        proxy_type = proxy_url.split("://")[0].replace("h", "")
        addr, port = proxy_url.split("://")[1].split(":")
        return {"proxy_type": proxy_type, "addr": addr, "port": int(port)}
    except Exception as e:
        logger.error(f"Failed to parse proxy {proxy_url!r}: {e}")
        return None


@dataclass
class AccountConfig:
    name: str
    api_id: int
    api_hash: str
    session: str
    proxy_url: str = ""
    manager_username: str = ""
    cold_dm_daily_limit: int = 5
    enabled: bool = True

    @property
    def proxy(self):
        return parse_proxy(self.proxy_url)


@dataclass
class LLMEndpoint:
    provider: str
    base_url: str
    api_key: str
    model: str
    extra_headers: dict = field(default_factory=dict)
    yandex_folder: str = ""

    def resolved_model(self):
        if self.provider == "yandex" and not self.model.startswith("gpt://"):
            return f"gpt://{self.yandex_folder}/{self.model}"
        return self.model


@dataclass
class Settings:
    target_chat: int
    bot_token: str
    bot_chat_id: str
    proxy_url: str
    manager_username: str
    product_url: str
    product_name: str
    cold_dm_daily_limit: int
    send_delay_min_seconds: int
    send_delay_max_seconds: int
    generic_limit_cooldown_seconds: int
    flood_wait_extra_seconds: int
    incoming_reply_debounce_seconds: int
    reply_daily_cap: int
    notify_retry_interval_seconds: int
    max_pitch_attempts: int
    db_path: str
    legacy_json_path: str
    accounts_file: str


def load_settings():
    return Settings(
        target_chat=_int_env("TARGET_CHAT", 0),
        bot_token=os.getenv("BOT_TOKEN", ""),
        bot_chat_id=os.getenv("BOT_CHAT_ID", ""),
        proxy_url=os.getenv("PROXY_URL", ""),
        manager_username=os.getenv("MANAGER_USERNAME", "MaksIgitov"),
        product_url=os.getenv("PRODUCT_URL", "https://pulsar-tg.ru/"),
        product_name=os.getenv("PRODUCT_NAME", "Пульсар"),
        cold_dm_daily_limit=_int_env("COLD_DM_DAILY_LIMIT", 5),
        send_delay_min_seconds=_int_env("SEND_DELAY_MIN_SECONDS", 120),
        send_delay_max_seconds=_int_env("SEND_DELAY_MAX_SECONDS", 600),
        generic_limit_cooldown_seconds=_int_env("GENERIC_LIMIT_COOLDOWN_SECONDS", 5 * 60 * 60),
        flood_wait_extra_seconds=_int_env("FLOOD_WAIT_EXTRA_SECONDS", 5 * 60),
        incoming_reply_debounce_seconds=_int_env("INCOMING_REPLY_DEBOUNCE_SECONDS", 20),
        reply_daily_cap=_int_env("REPLY_DAILY_CAP", 10),
        notify_retry_interval_seconds=_int_env("NOTIFY_RETRY_INTERVAL_SECONDS", 60),
        max_pitch_attempts=_int_env("MAX_PITCH_ATTEMPTS", 5),
        db_path=os.getenv("HERMES_DB", "hermes.db"),
        legacy_json_path=os.getenv("DB_FILE", "mtproto_leads.json"),
        accounts_file=os.getenv("ACCOUNTS_FILE", "accounts.yaml"),
    )


def load_accounts(settings):
    """Accounts come from accounts.yaml; falls back to legacy single-account env vars."""
    accounts = []
    for path in (settings.accounts_file, os.getenv("RUNTIME_ACCOUNTS_FILE", "runtime_accounts.yaml")):
        if path and os.path.exists(path):
            accounts.extend(_load_accounts_yaml(path, settings))
    names = [account.name for account in accounts]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate account names across account files")
    if accounts:
        return accounts

    api_id = _int_env("API_ID", 0)
    api_hash = os.getenv("API_HASH", "")
    session = os.getenv("SESSION", "")
    if not api_id or not api_hash or not session:
        return []
    return [
        AccountConfig(
            name=os.getenv("ACCOUNT_NAME", "main"),
            api_id=api_id,
            api_hash=api_hash,
            session=session,
            proxy_url=settings.proxy_url,
            manager_username=settings.manager_username,
            cold_dm_daily_limit=settings.cold_dm_daily_limit,
        )
    ]


def _load_accounts_yaml(path, settings):
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    accounts = []
    seen = set()
    for i, raw in enumerate(data.get("accounts") or []):
        raw = {k: _expand_env(v) for k, v in (raw or {}).items()}
        name = str(raw.get("name") or f"account{i + 1}")
        if name in seen:
            raise ValueError(f"Duplicate account name in {path}: {name}")
        seen.add(name)
        if not raw.get("enabled", True):
            continue
        try:
            account = AccountConfig(
                name=name,
                api_id=int(raw["api_id"]),
                api_hash=str(raw["api_hash"]),
                session=str(raw["session"]),
                proxy_url=str(raw.get("proxy") or settings.proxy_url),
                manager_username=str(raw.get("manager_username") or settings.manager_username),
                cold_dm_daily_limit=int(raw.get("cold_dm_daily_limit") or settings.cold_dm_daily_limit),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Invalid account entry {name!r} in {path}: {e}")
        if not account.api_id or not account.api_hash or not account.session:
            raise ValueError(f"Account {name!r} in {path} is missing api_id/api_hash/session")
        accounts.append(account)
    return accounts


# LLM providers. Every provider speaks the OpenAI-compatible chat completions
# protocol, so switching models is a config change, not a code change.
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"

_PROVIDER_DEFAULTS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": DEFAULT_OPENROUTER_MODEL,
    },
    "yandex": {
        "base_url": "https://llm.api.cloud.yandex.net/v1",
        "api_key_env": "YANDEX_CLOUD_API_KEY",
        "model_env": "YANDEX_CLOUD_MODEL",
        "default_model": "yandexgpt-5.1/latest",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
    },
    "custom": {
        "base_url": "",
        "api_key_env": "LLM_API_KEY",
        "model_env": "LLM_MODEL",
        "default_model": "",
    },
}


def build_endpoint(provider, model=None):
    provider = (provider or "").strip().lower()
    spec = _PROVIDER_DEFAULTS.get(provider)
    if not spec:
        raise ValueError(f"Unknown LLM provider: {provider!r}. Known: {sorted(_PROVIDER_DEFAULTS)}")

    base_url = os.getenv(f"{provider.upper()}_BASE_URL", spec["base_url"])
    if provider == "custom":
        base_url = os.getenv("LLM_BASE_URL", base_url)
    if not base_url:
        raise ValueError(f"Provider {provider!r} requires a base URL (LLM_BASE_URL)")

    resolved_model = model or os.getenv(spec["model_env"], "") or spec["default_model"]
    if not resolved_model:
        raise ValueError(f"Provider {provider!r} requires a model")

    extra_headers = {}
    if provider == "openrouter":
        referer = os.getenv("OPENROUTER_SITE_URL", "")
        title = os.getenv("OPENROUTER_APP_NAME", "hermes-botyara")
        if referer:
            extra_headers["HTTP-Referer"] = referer
        if title:
            extra_headers["X-Title"] = title

    return LLMEndpoint(
        provider=provider,
        base_url=base_url,
        api_key=os.getenv(spec["api_key_env"], ""),
        model=resolved_model,
        extra_headers=extra_headers,
        yandex_folder=os.getenv("YANDEX_CLOUD_FOLDER", ""),
    )


def build_llm_routes():
    """Per-task endpoint chains: primary endpoint plus optional fallback provider.

    LLM_PROVIDER selects the primary provider, LLM_MODEL overrides its model.
    LLM_PROVIDER_PITCH / LLM_MODEL_PITCH (and _CLASSIFY / _REPLY) override per task,
    which makes model experiments per pipeline stage a pure .env change.
    """
    default_provider = os.getenv("LLM_PROVIDER", "") or _infer_default_provider()
    default_model = os.getenv("LLM_MODEL", "") or None
    fallback_provider = os.getenv("LLM_FALLBACK_PROVIDER", "").strip().lower()
    fallback_model = os.getenv("LLM_FALLBACK_MODEL", "") or None

    routes = {}
    for task in LLM_TASKS:
        provider = os.getenv(f"LLM_PROVIDER_{task.upper()}", "") or default_provider
        model = os.getenv(f"LLM_MODEL_{task.upper()}", "") or default_model
        chain = [build_endpoint(provider, model)]
        if fallback_provider and fallback_provider != provider.strip().lower():
            try:
                chain.append(build_endpoint(fallback_provider, fallback_model))
            except ValueError as e:
                logger.warning(f"Skipping LLM fallback for task {task}: {e}")
        routes[task] = chain
    return routes


def _infer_default_provider():
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.getenv("YANDEX_CLOUD_API_KEY"):
        return "yandex"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "openrouter"
