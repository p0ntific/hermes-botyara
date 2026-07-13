import time
import logging

import openai

logger = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """Every endpoint in the task chain failed."""


class LLMResult:
    __slots__ = ("text", "provider", "model", "latency_ms")

    def __init__(self, text, provider, model, latency_ms):
        self.text = text
        self.provider = provider
        self.model = model
        self.latency_ms = latency_ms


class LLMRouter:
    """Routes chat completions per task through a chain of OpenAI-compatible endpoints.

    Chain semantics: try the primary endpoint (with retries), then fall through to
    the next endpoint. Covers OpenRouter, Yandex Cloud, OpenAI and anything custom
    with the same wire protocol (vLLM, Ollama, ...).
    """

    def __init__(self, routes, on_call=None, attempts_per_endpoint=2, client_factory=None):
        self.routes = routes
        self.on_call = on_call
        self.attempts_per_endpoint = max(1, attempts_per_endpoint)
        self._client_factory = client_factory or self._default_client_factory
        self._clients = {}

    @staticmethod
    def _default_client_factory(endpoint):
        return openai.OpenAI(
            api_key=endpoint.api_key or "unset",
            base_url=endpoint.base_url,
            default_headers=endpoint.extra_headers or None,
            timeout=60,
            max_retries=0,
        )

    def _client(self, endpoint):
        key = (endpoint.provider, endpoint.base_url, endpoint.api_key)
        if key not in self._clients:
            self._clients[key] = self._client_factory(endpoint)
        return self._clients[key]

    def describe(self, task):
        chain = self.routes.get(task) or []
        return " -> ".join(f"{e.provider}:{e.resolved_model()}" for e in chain)

    def chat(self, task, messages, temperature=0.3, max_tokens=1000, lead_key=None):
        chain = self.routes.get(task)
        if not chain:
            raise LLMUnavailable(f"No LLM route configured for task {task!r}")

        last_error = None
        for endpoint in chain:
            model = endpoint.resolved_model()
            for attempt in range(1, self.attempts_per_endpoint + 1):
                started = time.monotonic()
                try:
                    response = self._client(endpoint).chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    latency_ms = int((time.monotonic() - started) * 1000)
                    text = (response.choices[0].message.content or "").strip()
                    self._record(task, endpoint, True, latency_ms, None, lead_key)
                    return LLMResult(text, endpoint.provider, model, latency_ms)
                except Exception as e:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    last_error = e
                    self._record(task, endpoint, False, latency_ms, str(e)[:500], lead_key)
                    logger.warning(
                        f"LLM {task} failed on {endpoint.provider}:{model} "
                        f"(attempt {attempt}/{self.attempts_per_endpoint}): {e}"
                    )
        raise LLMUnavailable(f"All LLM endpoints failed for task {task!r}: {last_error}")

    def _record(self, task, endpoint, ok, latency_ms, error, lead_key):
        if not self.on_call:
            return
        try:
            self.on_call(
                task=task,
                provider=endpoint.provider,
                model=endpoint.resolved_model(),
                ok=ok,
                latency_ms=latency_ms,
                error=error,
                lead_key=lead_key,
            )
        except Exception:
            logger.exception("LLM call logger failed")
