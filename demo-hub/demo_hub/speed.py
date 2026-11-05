"""How fast a local model reads and writes on this machine, without the agent around it.

    python -m demo_hub.speed --model granite4.2:8b [--model ...] [--threads 4] [--url URL]

Each run sends the Assistant's own instructions plus the recipe-shopper procedure (about 1,500
tokens) behind a random first line, so nothing is cached and every token is read, then asks for
about 120 tokens. It reports Ollama's own counts: tokens read per second (the prompt) and written
per second (the answer), and the load time. Engine settings that need a server restart
(OLLAMA_FLASH_ATTENTION, OLLAMA_KV_CACHE_TYPE) are compared by pointing ``--url`` at a second
``ollama serve`` started with them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any

import httpx

from demo_hub.agent import PREAMBLE, load_skill
from demo_hub.settings import Settings

ASK = ("Write a short note, about 100 words, telling a shopper how you plan a recipe with these "
       "tools: which tool first, what you pass it, and what the answer contains.")


async def measure(client: httpx.AsyncClient, url: str, model: str, prompt: str,
                  options: dict[str, Any]) -> dict[str, Any]:
    body = {"model": model, "stream": False, "think": False, "keep_alive": "30m",
            "messages": [{"role": "system", "content": f"Session {uuid.uuid4().hex}.\n{prompt}"},
                         {"role": "user", "content": ASK}],
            "options": {"num_ctx": 16384, "num_predict": 120, "temperature": 0, **options}}
    started = time.perf_counter()
    response = await client.post(f"{url}/api/chat", json=body)
    response.raise_for_status()
    data = response.json()
    ns = 1_000_000_000
    read_s = (data.get("prompt_eval_duration") or 0) / ns
    write_s = (data.get("eval_duration") or 0) / ns
    return {"model": model, **options, "wall_s": round(time.perf_counter() - started, 1),
            "load_s": round((data.get("load_duration") or 0) / ns, 1),
            "read_tokens": data.get("prompt_eval_count"), "read_s": round(read_s, 1),
            "read_tok_s": round(data["prompt_eval_count"] / read_s, 1) if read_s else None,
            "write_tokens": data.get("eval_count"), "write_s": round(write_s, 1),
            "write_tok_s": round(data["eval_count"] / write_s, 2) if write_s else None}


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--threads", type=int, action="append", default=[],
                        help="num_thread values to try (default: Ollama's choice)")
    parser.add_argument("--url", default=Settings.from_env().ollama_url)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label", default="", help="a name for the server's settings")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    prompt = PREAMBLE + "\n# Recipe-shopper procedure\n\n" + load_skill(settings.recipe_shopper_skill)
    async with httpx.AsyncClient(timeout=1800) as client:
        for model in args.model:
            # one untimed call loads the model, so load time is not counted as reading
            await client.post(f"{args.url}/api/generate", json={
                "model": model, "prompt": "", "keep_alive": "30m",
                "options": {"num_ctx": 16384}})
            for threads in args.threads or [None]:
                for _ in range(args.repeat):
                    options = {"num_thread": threads} if threads else {}
                    result = await measure(client, args.url, model, prompt, options)
                    print(json.dumps({"server": args.label or args.url, **result}), flush=True)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
