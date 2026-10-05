"""Where the hub finds every service and secret. All of it comes from the environment so the
same code runs on any machine; ``scripts/up.sh`` sets these for the local stack."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_secret(path: str) -> str:
    """The first line of a secret file, or "" when it is missing. Never logged."""
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Settings:
    pantry_api_url: str = "http://127.0.0.1:8000"
    contextforge_url: str = "http://127.0.0.1:4444"
    fetch_url: str = "http://127.0.0.1:9100"
    mcpsim_ui_url: str = "http://127.0.0.1:8765"
    ollama_url: str = "http://127.0.0.1:11434"
    burr_url: str = "http://127.0.0.1:7241"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_api_key: str = field(default="", repr=False)
    # The hub's own bearer token for pantry's /mcp (a label in pantry's MCP_AUTH_TOKENS).
    pantry_mcp_token: str = field(default="", repr=False)
    pantry_token_label: str = "demo-hub"
    contextforge_jwt: str = field(default="", repr=False)
    spa_dist: str = ""
    recipe_shopper_skill: str = ""
    default_agent_model: str = "gemini:gemini-3-flash-preview"
    # Tried in order when the current model's daily free-tier quota is spent (each model has its own).
    agent_fallbacks: tuple[str, ...] = ("gemini:gemini-3-flash-preview", "gemini:gemini-flash-latest",
                                        "gemini:gemini-flash-lite-latest",
                                        "gemini:gemini-3.1-flash-lite")
    agent_max_steps: int = 12
    # The Assistant's tool disclosure: "progressive" (observers enable tools) or "all"; the
    # policy is a module:NAME written with the observers SDK. LLM observers are judged by
    # observer_model (one call per trigger, at most observer_max_calls per conversation;
    # "" leaves them unknown, so only code observers act).
    assistant_disclosure: str = "progressive"
    disclosure_policy: str = "demo_hub.assistant_policy:POLICY"
    observer_model: str = "gemini:gemini-flash-lite-latest"
    observer_max_calls: int = 12
    # Local models. Ollama's own default context (4,096 tokens) silently drops the start of the
    # Assistant's ~7k-token prompt, so the hub asks for a larger window on every call.
    ollama_num_ctx: int = 16384
    ollama_temperature: float | None = None  # None keeps the model's own default
    ollama_think: bool | None = None  # None leaves thinking at the model's default
    ollama_timeout_s: float = 900.0
    # The most of one tool result a local (ollama:) model reads; longer JSON is shrunk. On a CPU
    # it reads ~15-20 tokens a second, so 4,000 characters (~1,000 tokens) cost about a minute.
    local_result_chars: int = 4000
    # Every model call's timing, for the next call's estimate ("" keeps it in memory only).
    timings_path: str = ""

    @staticmethod
    def from_env() -> Settings:
        env = os.environ
        return Settings(
            pantry_api_url=env.get("PANTRY_API_URL", Settings.pantry_api_url).rstrip("/"),
            contextforge_url=env.get("CONTEXTFORGE_URL", Settings.contextforge_url).rstrip("/"),
            fetch_url=env.get("FETCH_URL", Settings.fetch_url).rstrip("/"),
            mcpsim_ui_url=env.get("MCPSIM_UI_URL", Settings.mcpsim_ui_url).rstrip("/"),
            ollama_url=env.get("OLLAMA_URL", Settings.ollama_url).rstrip("/"),
            burr_url=env.get("BURR_URL", Settings.burr_url).rstrip("/"),
            gemini_base_url=env.get("GEMINI_BASE_URL", Settings.gemini_base_url).rstrip("/"),
            gemini_api_key=env.get("GEMINI_API_KEY", ""),
            pantry_mcp_token=env.get("PANTRY_MCP_TOKEN")
            or _read_secret(env.get("PANTRY_MCP_TOKEN_FILE", "")),
            pantry_token_label=env.get("PANTRY_MCP_TOKEN_LABEL", Settings.pantry_token_label),
            contextforge_jwt=env.get("CONTEXTFORGE_JWT")
            or _read_secret(env.get("CF_JWT_FILE", "")),
            spa_dist=env.get("SPA_DIST", ""),
            recipe_shopper_skill=env.get("RECIPE_SHOPPER_SKILL", ""),
            default_agent_model=env.get("DEMO_AGENT_MODEL", Settings.default_agent_model),
            agent_fallbacks=tuple(m.strip() for m in env.get("DEMO_AGENT_FALLBACKS", "").split(",")
                                  if m.strip()) or Settings.agent_fallbacks,
            agent_max_steps=int(env.get("DEMO_AGENT_MAX_STEPS", str(Settings.agent_max_steps))),
            assistant_disclosure=env.get("DEMO_AGENT_DISCLOSURE", Settings.assistant_disclosure),
            disclosure_policy=env.get("DEMO_AGENT_POLICY", Settings.disclosure_policy),
            observer_model=env.get("DEMO_OBSERVER_MODEL", Settings.observer_model),
            observer_max_calls=int(env.get("DEMO_OBSERVER_MAX_CALLS",
                                           str(Settings.observer_max_calls))),
            ollama_num_ctx=int(env.get("OLLAMA_NUM_CTX", str(Settings.ollama_num_ctx))),
            local_result_chars=int(env.get("DEMO_LOCAL_RESULT_CHARS",
                                           str(Settings.local_result_chars))),
            ollama_temperature=float(env["OLLAMA_TEMPERATURE"]) if env.get("OLLAMA_TEMPERATURE")
            else None,
            ollama_think=_flag(env.get("OLLAMA_THINK", "")),
            ollama_timeout_s=float(env.get("OLLAMA_TIMEOUT_S", str(Settings.ollama_timeout_s))),
            timings_path=env.get("LLM_TIMINGS_PATH", "~/.pantry-demo/llm-timings.jsonl"),
        )


def _flag(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None
