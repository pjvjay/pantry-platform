"""The mcp-sim runner UI, used from the hub's Simulations tab.

The runner serves JSON at ``/api/*``. Its POSTs need the per-process token it prints into the
page (``<meta name="mcpsim-token">``) in an ``X-MCPSim-Token`` header; the hub reads it from the
page server-side, so the browser never needs it. Deep views (the full transcript, plan and
informant reports) stay in the runner, which the hub links to.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from demo_hub.settings import Settings

TOKEN_RE = re.compile(r'<meta name="mcpsim-token" content="([^"]+)"')
# The presets the Simulations tab offers. Free-tier Gemini quotas are per model per day, so the
# roles are spread across models. The local presets run on Ollama (the runner asks it for a
# 16k context: MCPSIM_OLLAMA_NUM_CTX in scripts/up.sh).
PRESETS: dict[str, dict[str, Any]] = {
    "gemini": {
        "label": "Gemini (free tier)",
        "models": {"planner": "gemini:gemini-3.1-flash-lite", "agent": "gemini:gemini-3-flash-preview",
                   "user": "gemini:gemini-flash-lite-latest",
                   "observer": "gemini:gemini-flash-lite-latest",
                   "judge": "gemini:gemini-3.1-flash-lite"},
        "allow_same_judge": False,
    },
    "hybrid": {
        "label": "Gemini agent, local Cohere for the rest",
        "models": {"planner": "ollama:command-r7b", "agent": "gemini:gemini-3-flash-preview",
                   "user": "ollama:command-r7b", "observer": "ollama:command-r7b",
                   "judge": "ollama:command-r7b"},
        "allow_same_judge": False,
    },
    "granite-agent": {
        "label": "Granite 4.2 8B agent (local), Gemini for the rest",
        "models": {"planner": "gemini:gemini-3.1-flash-lite", "agent": "ollama:granite4.2:8b",
                   "user": "gemini:gemini-flash-lite-latest",
                   "observer": "gemini:gemini-flash-lite-latest",
                   "judge": "gemini:gemini-3.1-flash-lite"},
        "allow_same_judge": False,
    },
    "granite": {
        "label": "All local Granite 4.2 8B (slow on a CPU: minutes per turn)",
        "models": {role: "ollama:granite4.2:8b"
                   for role in ("planner", "agent", "user", "observer", "judge")},
        "allow_same_judge": True,
    },
    "local": {
        "label": "All local Cohere command-r7b (slow on a CPU: minutes per turn)",
        "models": {role: "ollama:command-r7b"
                   for role in ("planner", "agent", "user", "observer", "judge")},
        "allow_same_judge": True,
    },
    "dry": {"label": "Dry run (no LLM)", "models": {}, "allow_same_judge": False,
            "dry_run": True},
}


class SimsError(RuntimeError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class SimsClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.base = settings.mcpsim_ui_url
        self._client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            response = await client.request(method, f"{self.base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise SimsError(f"the mcp-sim runner is not reachable at {self.base} ({exc})") \
                from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code >= 400:
            raise SimsError(f"mcp-sim runner answered {response.status_code}: "
                            f"{response.text[:300]}", response.status_code)
        return response

    async def get(self, path: str) -> Any:
        return (await self._request("GET", path)).json()

    async def token(self) -> str:
        page = (await self._request("GET", "/")).text
        match = TOKEN_RE.search(page)
        if not match:
            raise SimsError("the mcp-sim runner page carries no token")
        return match.group(1)

    async def start(self, scenarios: list[str] | str, preset: str, repeat: int = 1,
                    modes: list[str] | None = None) -> Any:
        if preset not in PRESETS:
            raise SimsError(f"unknown preset {preset!r}", 422)
        chosen = PRESETS[preset]
        body: dict[str, Any] = {"scenarios": scenarios, "repeat": repeat,
                                "dry_run": bool(chosen.get("dry_run")),
                                "allow_same_judge": chosen["allow_same_judge"]}
        if chosen["models"]:
            body["models"] = chosen["models"]
        if modes:
            body["modes"] = modes
        token = await self.token()
        response = await self._request("POST", "/api/run", json=body,
                                       headers={"X-MCPSim-Token": token})
        return response.json()

    async def cancel(self, job_id: str) -> Any:
        token = await self.token()
        response = await self._request("POST", f"/api/jobs/{job_id}/cancel",
                                        headers={"X-MCPSim-Token": token})
        return response.json()
