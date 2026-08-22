"""Gemini tool round-trip: the ``thought_signature`` contract.

WHY THIS FILE EXISTS. `4f0fadc` fixed a bug that made EVERY tool-using agent
turn fail on the product while plain chat worked. Gemini 3.x thinking models
attach a ``thought_signature`` to each function-call part and reject the
FOLLOW-UP request with ``400 INVALID_ARGUMENT ("Function call is missing a
thought_signature")`` if it is not echoed back. ``_to_contents`` rebuilt the
call with ``gt.Part.from_function_call(name, args)``, which drops it.

It is a nasty shape of bug: it lives in the SECOND request of a two-request
exchange, so anything that makes one call — a plain chat, a smoke test, a
`complete()` unit test — passes happily. It only appears when a tool is actually
invoked and the conversation continues. Nothing in the suite did that.

The guard here simulates the PROVIDER'S CONTRACT rather than our own data flow:
``_FakeGeminiAPI`` refuses the second request exactly as Google's endpoint does
when the signature is missing. That matters because asserting "we put the bytes
in the Part" only checks we did what we currently believe is required, whereas
refusing the request encodes WHY it is required — so the test still means
something if the plumbing is rewritten.

`test_the_sdk_still_drops_the_signature_on_from_function_call` pins the
third-party behaviour the workaround exists for. If a future google-genai
release preserves the signature, that test fails and tells us the manual Part
construction can be deleted — rather than leaving a mystery workaround behind
forever.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.genai import types as gt

from archon.llm import gemini as gemini_mod
from archon.llm.base import ChatMessage, ProviderError, ToolCall, ToolSpec

#: Stand-in for the real 265-byte opaque blob.
SIG = b"\x0a\x40" + b"signature-bytes" * 8

TOOL = ToolSpec(
    name="calendar_list_calendars",
    description="List the user's calendars.",
    input_schema={"type": "object", "properties": {}, "required": []},
)


class GeminiRejected(Exception):
    """Stands in for the SDK's 400 ClientError."""


class _FakeGeminiAPI:
    """Enforces Gemini 3.x's actual rule instead of trusting our own plumbing.

    Turn 1 answers with a function call carrying a signature. Turn 2 checks the
    conversation being replayed and refuses it — the way the real endpoint does
    — if any function-call part arrives without one.
    """

    def __init__(self, *, require_signature: bool = True) -> None:
        self.require_signature = require_signature
        self.calls: list[list[gt.Content]] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append(contents)

        for content in contents:
            for part in content.parts or []:
                if getattr(part, "function_call", None) is None:
                    continue
                if self.require_signature and not getattr(part, "thought_signature", None):
                    raise GeminiRejected(
                        "400 INVALID_ARGUMENT. Function call is missing a "
                        "thought_signature. Please ensure the thought_signature "
                        "is passed back to the model."
                    )

        usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=5,
                                cached_content_token_count=0)
        if len(self.calls) == 1:
            parts = [gt.Part(
                function_call=gt.FunctionCall(name=TOOL.name, args={}),
                thought_signature=SIG,
            )]
        else:
            parts = [gt.Part.from_text(text="You have 12 calendars.")]
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
            usage_metadata=usage,
        )


@pytest.fixture
def fake_api(monkeypatch) -> _FakeGeminiAPI:
    api = _FakeGeminiAPI()

    def _client(api_key: str):
        return SimpleNamespace(aio=SimpleNamespace(models=api))

    monkeypatch.setattr(gemini_mod.genai, "Client", _client)
    return api


def _provider() -> gemini_mod.GeminiProvider:
    return gemini_mod.GeminiProvider(api_key="fake")


def _round_trip(provider, history: list[ChatMessage]):
    return asyncio.run(provider.complete(
        model="gemini-3.7-flash", system="be helpful",
        messages=history, tools=[TOOL],
    ))


# --- the round trip ----------------------------------------------------------

def test_a_tool_using_turn_completes_end_to_end(fake_api):
    """The exact live failure: two Gemini calls with a tool in between.

    Before `4f0fadc` the second call raises the 400 and the whole agent turn
    dies. This is the shape no existing test had — one `complete()` call is
    never enough to see it.
    """
    provider = _provider()
    history: list[ChatMessage] = [ChatMessage(role="user", text="list my calendars")]

    first = _round_trip(provider, history)
    assert first.stop_reason == "tool_use"
    assert [tc.name for tc in first.tool_calls] == [TOOL.name]
    assert first.tool_calls[0].signature == SIG, "signature not carried off the wire"

    # The agent loop appends the model's call, then the tool's result.
    history.append(ChatMessage(role="assistant", text=first.text,
                               tool_calls=first.tool_calls))
    history.append(ChatMessage(role="tool_result", text='{"calendars": 12}',
                               tool_name=TOOL.name, tool_call_id=first.tool_calls[0].id))

    second = _round_trip(provider, history)
    assert second.stop_reason == "end"
    assert second.text == "You have 12 calendars."
    assert len(fake_api.calls) == 2, "the follow-up request never happened"


def test_the_replayed_part_carries_the_original_signature(fake_api):
    """Same flow, asserting on the bytes actually put on the wire."""
    provider = _provider()
    first = _round_trip(provider, [ChatMessage(role="user", text="go")])
    history = [
        ChatMessage(role="user", text="go"),
        ChatMessage(role="assistant", text="", tool_calls=first.tool_calls),
        ChatMessage(role="tool_result", text="{}", tool_name=TOOL.name),
    ]
    _round_trip(provider, history)

    replayed = [
        part
        for content in fake_api.calls[1]
        for part in (content.parts or [])
        if getattr(part, "function_call", None) is not None
    ]
    assert len(replayed) == 1
    assert replayed[0].thought_signature == SIG
    assert replayed[0].function_call.name == TOOL.name


def test_a_signature_free_provider_response_still_works(fake_api):
    """Not every model is a thinking model, and other providers set nothing.

    ``_to_contents`` must keep using the plain ``from_function_call`` path when
    there is no signature rather than constructing a Part with ``None``.
    """
    contents = gemini_mod._to_contents([
        ChatMessage(role="assistant", text="",
                    tool_calls=[ToolCall(id="1", name="t", args={"a": 1})]),
    ])
    part = contents[0].parts[0]
    assert part.function_call.name == "t"
    assert not getattr(part, "thought_signature", None)


def test_to_contents_preserves_the_signature_for_every_call_in_a_turn(fake_api):
    """A single turn can hold several function calls; all must keep theirs."""
    contents = gemini_mod._to_contents([
        ChatMessage(role="assistant", text="", tool_calls=[
            ToolCall(id="1", name="a", args={}, signature=b"sig-a"),
            ToolCall(id="2", name="b", args={}, signature=b"sig-b"),
        ]),
    ])
    sigs = [p.thought_signature for p in contents[0].parts]
    assert sigs == [b"sig-a", b"sig-b"]


# --- through the real agent loop ---------------------------------------------

class _RouterThroughGemini:
    """Minimal Router stand-in that calls the real GeminiProvider."""

    def __init__(self, provider) -> None:
        self.provider = provider

    async def complete(self, *, purpose, system, messages, tools,
                       max_tokens=4096, chat_pk=None):
        return await self.provider.complete(
            model="gemini-3.7-flash", system=system, messages=messages,
            tools=tools, max_tokens=max_tokens,
        )


def test_the_real_agent_loop_survives_a_tool_call(fake_api, tmp_path):
    """End-to-end through ``run_agent`` — the live path, minus the HTTP call.

    The tests above build the tool-loop history by hand, which quietly assumes
    that hand-built history matches what ``agent.py`` actually produces. This
    removes the assumption: the real loop appends the real ToolCall objects, the
    real registry dispatches the tool, and the real ``_to_contents`` serialises
    the follow-up. Only the transport is fake, so if the loop ever rebuilt a
    ToolCall and dropped the signature — a fix in gemini.py would NOT save it —
    this fails.
    """
    from archon.agent.agent import run_agent
    from archon.tools.registry import Registry, ToolContext

    from test_api import make_rt

    rt = make_rt(tmp_path)
    registry = Registry()

    @registry.tool(TOOL.name, TOOL.description, TOOL.input_schema)
    async def _list_calendars(ctx: ToolContext) -> str:
        return '{"calendars": ["work", "family"]}'

    answer = asyncio.run(run_agent(
        _RouterThroughGemini(_provider()), registry,
        ToolContext(rt=rt, scope="owner"),
        system="be helpful",
        messages=[ChatMessage(role="user", text="list my calendars")],
    ))

    assert answer == "You have 12 calendars."
    assert len(fake_api.calls) == 2

    replayed = [
        p for c in fake_api.calls[1] for p in (c.parts or [])
        if getattr(p, "function_call", None) is not None
    ]
    assert [p.thought_signature for p in replayed] == [SIG]


# --- mutation guards ---------------------------------------------------------

def test_dropping_the_signature_reproduces_the_live_400(fake_api, monkeypatch):
    """Revert `4f0fadc`'s replay and confirm this file catches it.

    Rebuilds the pre-fix ``_to_contents`` behaviour by forcing every ToolCall's
    signature to None, which is exactly what ``from_function_call`` produced.
    """
    provider = _provider()
    first = _round_trip(provider, [ChatMessage(role="user", text="go")])

    stripped = [ToolCall(id=tc.id, name=tc.name, args=tc.args)   # signature=None
                for tc in first.tool_calls]
    history = [
        ChatMessage(role="user", text="go"),
        ChatMessage(role="assistant", text="", tool_calls=stripped),
        ChatMessage(role="tool_result", text="{}", tool_name=TOOL.name),
    ]

    with pytest.raises(ProviderError) as exc:
        _round_trip(provider, history)
    # complete() wraps SDK errors, which is what hid the real cause on the box.
    assert "gemini API error" in str(exc.value)
    assert isinstance(exc.value.__cause__, GeminiRejected)
    assert "thought_signature" in str(exc.value.__cause__)


def test_the_sdk_still_drops_the_signature_on_from_function_call():
    """Pins the third-party behaviour the workaround exists for.

    ``gt.Part.from_function_call`` has no signature parameter, so the manual
    ``gt.Part(...)`` construction in ``_to_contents`` is load-bearing. If a
    google-genai upgrade ever preserves it, this fails — and that is the signal
    that the workaround can be removed, instead of it living on as folklore.
    """
    part = gt.Part.from_function_call(name="t", args={})
    assert not getattr(part, "thought_signature", None)

    manual = gt.Part(function_call=gt.FunctionCall(name="t", args={}),
                     thought_signature=SIG)
    assert manual.thought_signature == SIG


def test_the_error_wrapper_hides_the_cause_which_is_why_this_test_exists():
    """Documents why the live failure was hard to read.

    ``complete()`` re-raises as ``ProviderError(f"gemini API error: {type}")``,
    discarding the message — so the box showed "ClientError" with no hint that a
    signature was missing. The chained ``__cause__`` is the only place the real
    text survives; anyone debugging a Gemini failure should look there.
    """
    import inspect
    src = inspect.getsource(gemini_mod.GeminiProvider.complete)
    assert 'raise ProviderError(f"gemini API error: {type(exc).__name__}") from exc' in src
