"""The call-time estimator: what it learns from finished calls and what it predicts."""

from __future__ import annotations

from pathlib import Path

from demo_hub.timings import CallTiming, Estimate, TimingStore, common_prefix, remaining_s

M = "ollama:granite4.2:8b"


def cold(tokens: int = 6000, read_s: float = 300, out: int = 200, gen_s: float = 100) -> CallTiming:
    return CallTiming(model=M, wall_s=read_s + gen_s, prompt_chars=tokens * 4, prompt_tokens=tokens,
                      new_tokens_est=tokens, prompt_s=read_s, output_tokens=out, gen_s=gen_s)


def test_common_prefix() -> None:
    assert common_prefix("abcdef", "abcxyz") == 3
    assert common_prefix("", "abc") == 0 and common_prefix("same", "same") == 4


def test_there_is_always_an_estimate() -> None:
    store = TimingStore()
    est = store.estimate(M, 7200, 0, False)               # nothing at all: laptop-CPU defaults
    assert (est.read_tok_s, est.gen_tok_s, est.output_tokens) == (20.0, 2.0, 200)
    assert est.total_s == 2000 / 20 + 200 / 2 and "defaults" in est.basis
    assert store.estimate("gemini:m", 1000, 0, False).total_s == 10.0
    for wall in (2.0, 4.0, 30.0):
        store.record(CallTiming(model="gemini:m", wall_s=wall))
    est = store.estimate("gemini:m", 1000, 0, False)
    assert (est.total_s, est.samples, est.read_s) == (4.0, 3, 0.0)
    assert "3 earlier call(s)" in est.basis
    assert store.estimate("gemini:other", 1000, 0, False).basis == "other cloud models' calls"


def test_a_new_model_borrows_from_its_sibling_then_other_local_models() -> None:
    store = TimingStore()
    store.record(cold())                                  # granite with thinking on: 20 / 2 tok/s
    sibling = store.estimate(M + "#think=false", 24000, 0, False)
    assert (sibling.read_tok_s, sibling.gen_tok_s, sibling.output_tokens) == (20.0, 2.0, 200)
    assert sibling.basis == "other local models' calls (none of this model yet)"
    other = store.estimate("ollama:command-r7b", 24000, 0, False)
    assert other.read_tok_s == 20.0 and other.prompt_tokens == 6000     # its chars per token too


def test_a_cold_history_predicts_a_full_read_until_cache_reuse_is_seen() -> None:
    store = TimingStore()
    store.record(cold())                                  # 20 tok/s reading, 2 tok/s writing
    assert store.read_tok_s(M) == 20 and store.gen_tok_s(M) == 2 and store.chars_per_token(M) == 4
    assert store.reuses_cache(M) is None
    est = store.estimate(M, 24000, 20000, False)          # most of it could be cached, but unproven
    assert est.basis == "1 earlier call(s) of this model"
    assert est is not None and est.new_tokens == 6000 and est.read_s == 300 and est.gen_s == 100

    # A later call that could reuse 5,000 cached tokens read its prompt in 15 s: a caching model.
    store.record(CallTiming(model=M, wall_s=200, prompt_chars=26000, prompt_tokens=6500,
                            new_tokens_est=500, prompt_s=15, output_tokens=400, gen_s=200, later=True))
    assert store.reuses_cache(M) is True
    est = store.estimate(M, 28000, 26000, True)
    assert est is not None and est.new_tokens == 500 and est.read_s == 25.0
    assert est.output_tokens == 400                       # later calls' own reply length


def test_a_model_that_rereads_everything_is_recognised() -> None:
    store = TimingStore()
    store.record(cold(tokens=4800, read_s=240))
    store.record(CallTiming(model=M, wall_s=300, prompt_chars=20000, prompt_tokens=5000,
                            new_tokens_est=300, prompt_s=250, output_tokens=100, gen_s=50, later=True))
    assert store.reuses_cache(M) is False
    est = store.estimate(M, 21000, 20000, True)
    assert est is not None and est.new_tokens == est.prompt_tokens == 5250


def test_history_is_shared_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "timings.jsonl"
    writer, reader = TimingStore(str(path)), TimingStore(str(path))
    writer.record(cold())
    path.write_text(path.read_text() + '{"broken": \n')    # a half-written line from another process
    reader.reload()
    assert len(reader.calls[M]) == 1 and reader.calls[M][0].at
    assert len(TimingStore(str(path)).calls[M]) == 1


def test_remaining_time_while_reading_and_writing() -> None:
    est = Estimate(total_s=400, read_s=300, gen_s=100, output_tokens=200, gen_tok_s=2.0)
    assert remaining_s(est, 0, None, 0) == 400
    assert remaining_s(est, 350, None, 0) == 100           # reading took longer than estimated
    # Writing for 10 s at a live 3 tok/s with 30 of 200 tokens done.
    assert round(remaining_s(est, 310, 300, 30), 1) == round(170 / 3, 1)
    # Past the expected length the estimate stretches instead of reaching zero.
    assert remaining_s(est, 500, 300, 400) > 0


def test_last_prompts_are_shared_between_processes(tmp_path: Path) -> None:
    path = str(tmp_path / "timings.jsonl")
    hub, bench = TimingStore(path), TimingStore(path)
    assert hub.last_prompt("granite4.2:8b") == ""
    bench.set_last_prompt("granite4.2:8b", "tools+messages")
    assert hub.last_prompt("granite4.2:8b") == "tools+messages"
    memory = TimingStore()
    memory.set_last_prompt("m", "p")
    assert memory.last_prompt("m") == "p"


def test_a_call_with_an_unknown_cache_state_teaches_nothing_about_reading() -> None:
    store = TimingStore()
    store.record(cold())                                                  # 20 tok/s
    # The hub did not know what the cache held: 6,700 "new" tokens in 9 s was a cache hit.
    store.record(CallTiming(model=M, wall_s=37, prompt_chars=26800, prompt_tokens=6700,
                            new_tokens_est=6700, prompt_s=9.4, output_tokens=43, gen_s=27,
                            cache_known=False))
    assert store.read_tok_s(M) == 20 and store.gen_tok_s(M) is not None
