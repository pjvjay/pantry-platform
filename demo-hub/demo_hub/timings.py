"""How long a model call will take, learned from the calls before it.

A local model's call has two phases: reading the prompt (silent, and the long one on a CPU) and
writing the reply. Every finished call is recorded (``llm-timings.jsonl``, shared by the hub and
the model bench), and the next call is estimated from them:

- reading: the tokens the model must actually read over its measured reading speed. A model whose
  cache keeps the previous prompt (Granite) only reads what changed since its last call; one that
  re-reads everything every time (command-r7b, sliding-window attention) reads the whole prompt.
  Which kind a model is, is measured too.
- writing: the usual reply length for this kind of call (a conversation's first call, or a later
  one) over the measured writing speed; once tokens stream in, the live speed and count take over.

Gemini replies have no phases: its estimate is the median of its recent calls' wall time.

There is always an estimate. A number this model has no history for yet comes from the same model
with the other thinking setting, then from other local (or cloud) models, then from defaults for
an 8B model on a laptop CPU; ``basis`` says which.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KEEP_PER_MODEL = 200
DEFAULT_CHARS_PER_TOKEN = 3.6
MIN_READ_TOKENS = 500       # calls reading fewer tokens than this say little about reading speed
CACHED_ENOUGH = 1000        # a call that could reuse this many cached tokens shows whether it did
# With no history at all: an 8B model on a laptop CPU, and a cloud model's reply.
DEFAULT_READ_TOK_S = 20.0
DEFAULT_GEN_TOK_S = 2.0
DEFAULT_OUTPUT_TOKENS = 200
DEFAULT_WALL_S = 10.0


@dataclass
class CallTiming:
    """One finished model call."""

    model: str               # the model key: provider:model, plus #think=... when set
    wall_s: float
    prompt_chars: int = 0    # the serialized tools and messages sent
    prompt_tokens: int = 0   # the whole prompt as the model counts it, a cached prefix included
    new_tokens_est: int = 0  # the tokens it had to read if its cache kept the previous prompt
    prompt_s: float = 0.0
    output_tokens: int = 0
    gen_s: float = 0.0
    later: bool = False      # not the first call of a conversation
    # Whether the hub knew what the model's cache held (the prompt it last read). When it did not,
    # a fast read may have been a cache hit, so the call says nothing about reading speed.
    cache_known: bool = True
    at: str = ""


@dataclass
class Estimate:
    total_s: float
    read_s: float = 0.0
    gen_s: float = 0.0
    prompt_tokens: int = 0
    new_tokens: int = 0
    output_tokens: int = 0
    gen_tok_s: float = 0.0
    read_tok_s: float = 0.0
    samples: int = 0
    reuses_cache: bool | None = None
    basis: str = ""

    def public(self) -> dict[str, Any]:
        return {"estimate_s": round(self.total_s, 1), "read_s": round(self.read_s, 1),
                "gen_s": round(self.gen_s, 1), "prompt_tokens_est": self.prompt_tokens,
                "new_tokens_est": self.new_tokens, "output_tokens_est": self.output_tokens,
                "read_tok_s": round(self.read_tok_s, 1), "gen_tok_s": round(self.gen_tok_s, 2),
                "samples": self.samples, "basis": self.basis}


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    lo, hi = 0, n
    while lo < hi:                      # binary search on prefix equality: prompts are long
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


class TimingStore:
    def __init__(self, path: str = "") -> None:
        self.path = Path(path).expanduser() if path else None
        self.calls: dict[str, deque[CallTiming]] = defaultdict(lambda: deque(maxlen=KEEP_PER_MODEL))
        self._loaded_bytes = 0
        self._last: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        """Pick up calls other processes appended (the bench and the hub share the file)."""
        if not self.path or not self.path.exists():
            return
        with self.path.open("rb") as fh:
            fh.seek(self._loaded_bytes)
            chunk = fh.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self._loaded_bytes += len(complete)
        fields = set(CallTiming.__dataclass_fields__)
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
                call = CallTiming(**{k: v for k, v in row.items() if k in fields})
            except (ValueError, TypeError):
                continue
            self.calls[call.model].append(call)

    def _prompt_file(self, model: str) -> Path | None:
        if not self.path:
            return None
        digest = hashlib.sha256(model.encode()).hexdigest()[:16]
        return self.path.parent / "llm-last-prompts" / f"{digest}.txt"

    def last_prompt(self, model: str) -> str:
        """The prompt last sent to this Ollama model by any process sharing the file: what the
        model's cache holds now, as far as the hub and the bench know."""
        path = self._prompt_file(model)
        if path is None:
            return self._last.get(model, "")
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def set_last_prompt(self, model: str, prompt: str) -> None:
        self._last[model] = prompt
        path = self._prompt_file(model)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(prompt, encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass

    def record(self, call: CallTiming) -> None:
        call.at = call.at or datetime.now(UTC).isoformat(timespec="seconds")
        if self.path:
            try:
                self.reload()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                line = (json.dumps(asdict(call)) + "\n").encode("utf-8")
                with self.path.open("ab") as fh:
                    fh.write(line)
                self._loaded_bytes += len(line)
            except OSError:
                pass   # timing history is a convenience: never fail a call over it
        self.calls[call.model].append(call)

    # --- what the history says about a model ----------------------------------------------------

    def chars_per_token(self, model: str) -> float:
        ratios = [c.prompt_chars / c.prompt_tokens for c in self.calls[model]
                  if c.prompt_chars and c.prompt_tokens]
        return statistics.median(ratios) if ratios else DEFAULT_CHARS_PER_TOKEN

    def read_tok_s(self, model: str) -> float | None:
        """Reading speed, from calls that had to read (nearly) their whole prompt."""
        cold = [c.prompt_tokens / c.prompt_s for c in self.calls[model]
                if c.cache_known and c.prompt_s > 0 and c.prompt_tokens >= MIN_READ_TOKENS
                and c.new_tokens_est >= 0.9 * c.prompt_tokens]
        return statistics.median(cold) if cold else None

    def reuses_cache(self, model: str) -> bool | None:
        """Whether calls that could have reused a cached prompt took much less than a full read.
        None until there is evidence; estimates then assume a full read (too long beats stuck)."""
        rate = self.read_tok_s(model)
        could = [c for c in self.calls[model] if c.cache_known and c.prompt_s > 0
                 and c.prompt_tokens - c.new_tokens_est >= CACHED_ENOUGH]
        if not rate or not could:
            return None
        fast = sum(c.prompt_s < 0.5 * c.prompt_tokens / rate for c in could)
        return fast * 2 >= len(could)

    def gen_tok_s(self, model: str) -> float | None:
        recent = [c for c in list(self.calls[model])[-50:] if c.gen_s > 0 and c.output_tokens]
        seconds = sum(c.gen_s for c in recent)
        return sum(c.output_tokens for c in recent) / seconds if seconds else None

    def output_tokens(self, model: str, later: bool) -> int | None:
        same = [c.output_tokens for c in self.calls[model] if c.later == later and c.output_tokens]
        every = [c.output_tokens for c in self.calls[model] if c.output_tokens]
        pool = same or every
        return round(statistics.median(pool)) if pool else None

    def _peers(self, model: str) -> list[str]:
        """Models whose history stands in for this one's gaps: the same model with another
        thinking setting first (same reading and writing speed), then the rest of its kind."""
        base, local = model.split("#")[0], model.startswith("ollama:")
        same = [m for m in self.calls if m != model and m.split("#")[0] == base and self.calls[m]]
        kind = [m for m in self.calls if m != model and m not in same and self.calls[m]
                and m.startswith("ollama:") == local]
        return same + kind

    def _learned(self, model: str, measure: Any) -> tuple[Any, str]:
        """``measure(model)`` from this model, else its first peer that has it, else None."""
        value = measure(model)
        if value is not None:
            return value, "own"
        for peer in self._peers(model):
            value = measure(peer)
            if value is not None:
                return value, "peer"
        return None, "default"

    def estimate(self, model: str, prompt_chars: int, cached_chars: int, later: bool) -> Estimate:
        calls = self.calls[model]
        if not model.startswith("ollama:"):
            walls = [c.wall_s for c in list(calls)[-20:]]
            peer_walls = [c.wall_s for m in self._peers(model) for c in list(self.calls[m])[-20:]]
            pool = walls or peer_walls
            basis = (f"{len(walls)} earlier call(s) of this model" if walls else
                     "other cloud models' calls" if peer_walls else "a default guess (no history)")
            return Estimate(total_s=statistics.median(pool) if pool else DEFAULT_WALL_S,
                            samples=len(pool), basis=basis)
        read_rate, read_from = self._learned(model, self.read_tok_s)
        gen_rate, gen_from = self._learned(model, self.gen_tok_s)
        out_tokens, out_from = self._learned(model, lambda m: self.output_tokens(m, later))
        reuse, _ = self._learned(model, self.reuses_cache)
        cpt = self.chars_per_token(model) if any(c.prompt_chars for c in calls) else \
            self._learned(model, lambda m: self.chars_per_token(m) if any(
                c.prompt_chars for c in self.calls[m]) else None)[0] or DEFAULT_CHARS_PER_TOKEN
        read_rate, gen_rate = read_rate or DEFAULT_READ_TOK_S, gen_rate or DEFAULT_GEN_TOK_S
        out_tokens = out_tokens or DEFAULT_OUTPUT_TOKENS
        sources = {read_from, gen_from, out_from}
        basis = (f"{len(calls)} earlier call(s) of this model" if sources == {"own"} else
                 "defaults for an 8B model on a laptop CPU (no history yet)" if sources == {"default"}
                 else f"{len(calls)} earlier call(s) of this model, gaps filled from other local models"
                 if calls else "other local models' calls (none of this model yet)")
        prompt_tokens = round(prompt_chars / cpt)
        new_tokens = round(max(prompt_chars - cached_chars, 0) / cpt) if reuse else prompt_tokens
        read_s, gen_s = new_tokens / read_rate, out_tokens / gen_rate
        return Estimate(total_s=read_s + gen_s, read_s=read_s, gen_s=gen_s, prompt_tokens=prompt_tokens,
                        new_tokens=new_tokens, output_tokens=out_tokens, gen_tok_s=gen_rate,
                        read_tok_s=read_rate, samples=len(calls), reuses_cache=reuse, basis=basis)


def remaining_s(estimate: Estimate, elapsed: float, writing_since: float | None, tokens: int) -> float:
    """Seconds left: while reading, what is left of the estimated read plus the writing; once
    writing, the tokens still expected at the live writing speed."""
    if writing_since is None:
        return max(estimate.read_s - elapsed, 0.0) + estimate.gen_s
    writing_for = elapsed - writing_since
    rate = tokens / writing_for if tokens >= 5 and writing_for > 0 else estimate.gen_tok_s
    expected = max(estimate.output_tokens, round(tokens * 1.2))
    return (expected - tokens) / rate if rate else 0.0
