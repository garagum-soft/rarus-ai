"""A rate limit's reset, read from the vendor's own words; waited out in the turn, or resumed after it.

Both behaviours are settings that are off by default, and the default path --
the provider's short ladder, then the loop's longer one -- is pinned here too.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.providers.base import LLMProvider
from raven.providers.rate_limits import parse_duration, reset_delay

NOW = 1_800_000_000.0


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("6m12s", 372),
        ("1h 2m", 3720),
        ("20 seconds", 20),
        ("850ms", 0.85),
        ("1.5s", 1.5),
    ],
)
def test_durations_read_in_the_forms_vendors_write(text: str, seconds: float) -> None:
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"Retry-After": "30"}, 30),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"Retry-After": "Wed, 15 Jan 2027 08:00:30 GMT"}, None),  # computed below
        ({"x-ratelimit-reset-requests": "1s", "x-ratelimit-reset-tokens": "6m0s"}, 360),
        ({"anthropic-ratelimit-tokens-reset": datetime.fromtimestamp(NOW + 90, UTC).isoformat()}, 90),
        ({"x-ratelimit-reset": str(int(NOW + 45))}, 45),
        ({"x-ratelimit-reset": "12"}, 12),
    ],
)
def test_reset_headers_of_every_vendor(headers: dict, seconds: float | None) -> None:
    error = SimpleNamespace(response=SimpleNamespace(headers=headers))
    if seconds is None:
        target = datetime(2027, 1, 15, 8, 0, 30, tzinfo=UTC).timestamp()
        seconds = target - NOW

    assert reset_delay(error, "", now=NOW) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("Rate limit reached for gpt-4o. Please try again in 6m12s.", 372),
        ("429 Too Many Requests: retry after 30 seconds", 30),
        ('{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{"retryDelay": "33s"}]}}', 33),
        (f"Claude AI usage limit reached|{int(NOW + 7200)}", 7200),
        ("Your limit resets at 2027-01-15T08:00:00Z", datetime(2027, 1, 15, 8, tzinfo=UTC).timestamp() - NOW),
    ],
)
def test_reset_from_the_errors_own_words(message: str, seconds: float) -> None:
    assert reset_delay(None, message, now=NOW) == pytest.approx(seconds)


def test_a_clock_time_without_a_date_is_the_next_one() -> None:
    local = datetime.fromtimestamp(NOW).astimezone()
    two_hours_on = local + timedelta(hours=2)
    hour12 = (two_hours_on.hour % 12) or 12
    suffix = "pm" if two_hours_on.hour >= 12 else "am"
    delay = reset_delay(None, f"5-hour limit reached. Resets {hour12}{suffix}", now=NOW)

    assert delay is not None and 3600 < delay <= 2 * 3600


def test_nothing_said_is_no_answer() -> None:
    assert reset_delay(None, "Rate limit exceeded", now=NOW) is None


class _HTTPError(Exception):
    status_code = 429
    response = SimpleNamespace(headers={"retry-after": "42"})


def test_the_classification_carries_the_reset_and_names_a_subscription_running_out() -> None:
    with_header = _HTTPError("Too Many Requests")

    limited = LLMProvider.classify_error(with_header)
    subscription = LLMProvider.classify_error(content=f"Claude AI usage limit reached|{int(time.time()) + 600}")
    other = LLMProvider.classify_error(content="connection refused")

    assert limited.category == "rate_limit" and limited.retry_after_s == pytest.approx(42)
    assert subscription.category == "rate_limit" and subscription.should_fallback
    assert 590 <= subscription.retry_after_s <= 600
    assert other.retry_after_s is None


# -- the loop --------------------------------------------------------------------------------


class _Limited(LLMProvider):
    """A model that is rate limited ``limits`` times, each saying it lifts in ``reset`` seconds."""

    def __init__(self, limits: int, reset: float) -> None:
        super().__init__(api_key="k")
        self.limits, self.reset, self.calls = limits, reset, []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        from raven.contracts.llm_provider import LLMResponse

        self.calls.append(time.monotonic())
        if len(self.calls) <= self.limits:
            return LLMResponse(
                content=f"Error: Rate limit reached. Please try again in {self.reset}s.", finish_reason="error"
            )
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "m"

    @property
    def name(self) -> str:
        return "limited"


def _loop(tmp_path: Path, provider, **defaults):
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring
    from raven.agent.loop.recovery import RecoveryLimits
    from tests.test_agent_loop_model_selection import _stub_edges

    loop = AgentLoop(provider=provider, workspace=tmp_path, model="m", engine=EngineWiring())
    loop._recovery_limits = RecoveryLimits(llm_error_retry_delays=(0.01,), **defaults)
    _stub_edges(loop)
    return loop


def _req(text: str, channel: str = "telegram", chat: str = "42"):
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel=channel, chat_id=chat, sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation=f"{channel}:{chat}",
    )


async def test_by_default_a_rate_limit_takes_the_ladder_and_the_turn_fails_as_before(tmp_path: Path) -> None:
    from raven.spine.turn import AnswerlessTurnError

    provider = _Limited(limits=99, reset=0.3)
    loop = _loop(tmp_path, provider)

    with pytest.raises(AnswerlessTurnError) as failed:
        await loop._process_message(_req("hello"))

    assert "continues by itself" not in str(failed.value)
    from raven.proactive_engine.rate_limit_resume import ResumeStore

    assert ResumeStore().pending() == []


async def test_a_near_reset_is_waited_out_to_the_providers_word(tmp_path: Path) -> None:
    # Four limited calls: the provider's own ladder (three retries) runs out on them,
    # so the fifth is the loop asking again after its wait.
    provider = _Limited(limits=4, reset=0.3)
    loop = _loop(tmp_path, provider, rate_limit_wait_max_s=5)

    reply = await loop._process_message(_req("hello"))

    assert "done" in str(getattr(reply, "content", reply))
    calls = provider.calls
    assert len(calls) == 5
    # The loop waited the stated 0.3 s, plus its second of margin, before asking again.
    assert calls[4] - calls[3] >= 1.2


async def test_a_far_reset_ends_the_turn_and_schedules_the_conversation_to_continue(tmp_path: Path) -> None:
    from raven.proactive_engine.rate_limit_resume import ResumeStore
    from raven.spine.turn import AnswerlessTurnError

    provider = _Limited(limits=99, reset=3600)
    loop = _loop(tmp_path, provider, rate_limit_wait_max_s=5, rate_limit_resume=True)
    started = time.monotonic()

    with pytest.raises(AnswerlessTurnError) as failed:
        await loop._process_message(_req("write the quarterly report"))

    (entry,) = ResumeStore().pending()
    assert "this conversation continues by itself then" in str(failed.value)
    assert (entry.conversation, entry.channel, entry.chat_id) == ("telegram:42", "telegram", "42")
    assert entry.request == "write the quarterly report" and entry.chain == 1
    assert 3600 <= entry.due_at - time.time() <= 3600 + 31 + 5
    assert time.monotonic() - started < 3, "the loop's ladder was skipped: it could not outlast an hour"


async def test_a_one_shot_cli_run_is_not_resumed(tmp_path: Path) -> None:
    from raven.proactive_engine.rate_limit_resume import ResumeStore
    from raven.spine.turn import AnswerlessTurnError

    loop = _loop(tmp_path, _Limited(limits=99, reset=3600), rate_limit_resume=True)

    with pytest.raises(AnswerlessTurnError):
        await loop._process_message(_req("hello", channel="cli", chat="direct"))

    assert ResumeStore().pending() == []


async def test_the_resumer_continues_the_same_conversation_and_stops_after_three_in_a_row(tmp_path: Path) -> None:
    from raven.proactive_engine import rate_limit_resume as resume
    from raven.spine.turn import AnswerlessTurnError, Origin

    store = resume.ResumeStore()
    resume.schedule(
        conversation="telegram:42",
        channel="telegram",
        chat_id="42",
        delay_s=0,
        request="the report",
        now=time.time() - 60,
    )
    submitted = []
    resumer = resume.RateLimitResumer(submitted.append, store)

    resumer.resume_due(now=time.time())

    (turn,) = submitted
    assert turn.origin is Origin.SENTINEL and turn.conversation == "telegram:42"
    assert turn.source.channel == "telegram" and turn.source.sender_id == resume.SENDER
    assert turn.text.startswith("[Resumed by Raven, 1 of 3]") and "the report" in turn.text
    assert store.pending() == []

    # A resumed turn that hits the limit again schedules the next one in the chain...
    loop = _loop(tmp_path, _Limited(limits=99, reset=3600), rate_limit_resume=True)
    again = _req(turn.text)
    again = type(again)(**{**again.__dict__, "origin": Origin.SENTINEL, "source": turn.source})
    with pytest.raises(AnswerlessTurnError):
        await loop._process_message(again)
    assert store.pending()[0].chain == 2

    # ...and the third in a row is the last.
    third = type(again)(**{**again.__dict__, "text": turn.text.replace("1 of 3", "3 of 3")})
    with pytest.raises(AnswerlessTurnError) as failed:
        await loop._process_message(third)
    assert "stopped this conversation too often" in str(failed.value)


class _StreamLimited(LLMProvider):
    """Streams nothing but a 429 that says when it lifts, ``limits`` times; then answers."""

    def __init__(self, limits: int, reset: float) -> None:
        super().__init__(api_key="k")
        self.limits, self.reset, self.calls = limits, reset, []

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, *args, **kwargs):  # pragma: no cover - the stream path is under test
        raise NotImplementedError

    async def chat_stream(self, *args, **kwargs):
        from raven.providers.base import ChatDelta

        self.calls.append(time.monotonic())
        if len(self.calls) <= self.limits:
            error = _HTTPError("Too Many Requests")
            error.response = SimpleNamespace(headers={"retry-after": str(self.reset)})
            raise error
        yield ChatDelta(content="done")


async def _stream(provider, **kw):
    from raven.providers.streaming import stream_llm_call

    return await stream_llm_call(provider, messages=[{"role": "user", "content": "hi"}], tools=None, model="m", **kw)


async def test_a_streamed_rate_limit_takes_the_ladder_by_default() -> None:
    provider = _StreamLimited(limits=99, reset=0.2)

    with pytest.raises(_HTTPError):
        await _stream(provider, retry_delays=(0.01,))

    assert len(provider.calls) == 3, "two reconnect attempts and one ladder wait, as before"


async def test_a_streamed_rate_limit_near_its_reset_is_waited_out() -> None:
    provider = _StreamLimited(limits=2, reset=0.2)

    response = await _stream(provider, retry_delays=(), max_reconnects=1, rate_limit_wait_max_s=5)

    assert response.content == "done" and len(provider.calls) == 3
    assert provider.calls[2] - provider.calls[1] >= 1.1


async def test_a_streamed_rate_limit_too_far_off_comes_back_for_the_loop_to_resume() -> None:
    provider = _StreamLimited(limits=99, reset=3600)

    response = await _stream(provider, retry_delays=(0.01,), rate_limit_resume=True)

    assert response.finish_reason == "error" and response.error_classification.retry_after_s == pytest.approx(3600)
    assert len(provider.calls) == 2, "no ladder wait was spent on a limit an hour away"


async def test_a_streamed_turn_stopped_by_the_limit_is_scheduled_to_continue(tmp_path: Path) -> None:
    from raven.proactive_engine.rate_limit_resume import ResumeStore
    from raven.spine.events import NoticeKind
    from raven.spine.turn import AnswerlessTurnError

    loop = _loop(tmp_path, _StreamLimited(limits=99, reset=3600), rate_limit_resume=True)
    notices: list[tuple] = []

    async def on_delta(_text: str) -> None:
        return None

    async def on_notice(kind, text) -> None:
        notices.append((kind, text))

    with pytest.raises(AnswerlessTurnError) as failed:
        await loop._process_message(_req("write the quarterly report"), on_token_delta=on_delta, on_notice=on_notice)

    assert "continues by itself then" in str(failed.value)
    assert ResumeStore().pending()[0].conversation == "telegram:42"
    # A channel reports a failed model call by its category alone; the note is its own row.
    (note,) = [text for kind, text in notices if kind is NoticeKind.PROGRESS]
    assert note.startswith("The model provider's rate limit stopped this turn. It lifts by ")


def test_notices_are_a_runtime_name_of_the_turn_path() -> None:
    """The organ-degraded notice named it at runtime while it was imported for type checking only."""
    from raven.agent.loop import turn_path
    from raven.spine.events import NoticeKind

    assert turn_path.NoticeKind is NoticeKind


def test_the_resumer_is_built_only_with_resuming_on() -> None:
    from raven.config.schema import Config
    from raven.core.proactive_stack import build_rate_limit_resumer

    assert build_rate_limit_resumer(Config(), print) is None
    on = Config.model_validate({"agents": {"defaults": {"rateLimitResume": True}}})
    assert build_rate_limit_resumer(on, print) is not None
    assert Config().agents.defaults.rate_limit_wait_max_s == 0 and Config().agents.defaults.rate_limit_resume is False
