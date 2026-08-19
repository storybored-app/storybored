# OWNED-BY: llm-agent
"""OpenAI-compatible chat-completion client.

Config comes from env (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL) with DB
overrides from the `setting` table winning when set (Settings screen).
Unconfigured → LLMNotConfiguredError, which the API maps to a clear 503.
"""

from dataclasses import dataclass

import httpx
from sqlmodel import Session

from storybored.config import Settings
from storybored.settings_store import effective_setting, resolve_setting

#: generous default — local models can be slow on long scripts
DEFAULT_TIMEOUT = 300.0


class LLMError(RuntimeError):
    """Any LLM transport/response failure (mapped to 502 by the API)."""


class LLMNotConfiguredError(LLMError):
    """No LLM_BASE_URL configured (mapped to 503 by the API)."""


@dataclass
class LLMConfig:
    base_url: str
    api_key: str = ""
    model: str = ""
    #: Ollama's per-request keep_alive ("" = don't send the field). Set via
    #: the llm_keep_alive setting; "0" frees VRAM right after each call on a
    #: GPU shared with the render engine. Sent ONLY when non-empty — it's an
    #: explicit opt-in, because strict OpenAI-compatible providers may reject
    #: unknown request fields. Non-Ollama users simply leave it unset.
    keep_alive: str = ""


def get_llm_config(session: Session, settings: Settings) -> LLMConfig:
    """Resolve effective LLM config (DB override > env). Raises when unset.

    Key-exfil guard: if the base URL comes from a runtime DB override (a host
    the environment never configured), the env-provided API key is NOT attached
    unless the key was ALSO supplied as an override — so the env secret is never
    sent to an attacker-chosen endpoint set via PUT /api/settings.
    """
    base_url, base_source = resolve_setting(session, settings, "llm_base_url")
    if not base_url:
        raise LLMNotConfiguredError(
            "Script breakdown is not configured — set an LLM endpoint "
            "(LLM_BASE_URL and LLM_MODEL) in Settings or .env."
        )
    api_key, key_source = resolve_setting(session, settings, "llm_api_key")
    if base_source == "override" and key_source != "override":
        api_key = ""
    return LLMConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=effective_setting(session, settings, "llm_model"),
        keep_alive=effective_setting(session, settings, "llm_keep_alive").strip(),
    )


def _keep_alive_value(raw: str) -> int | str:
    """Ollama accepts a number (seconds; 0/-1 special) or a duration string
    ("5m"). Send integers as integers so "0" means "unload now", not a
    string the server has to parse."""
    try:
        return int(raw)
    except ValueError:
        return raw


def chat(
    config: LLMConfig,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """POST /chat/completions, return the assistant message content."""
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = {
        "model": config.model or "default",
        "messages": messages,
        "temperature": temperature,
    }
    if config.keep_alive:
        payload["keep_alive"] = _keep_alive_value(config.keep_alive)
    url = f"{config.base_url}/chat/completions"
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise LLMError(f"LLM returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError("LLM response was not an OpenAI-compatible chat completion") from exc
    if not isinstance(content, str):
        raise LLMError("LLM response content was not text")
    return content
