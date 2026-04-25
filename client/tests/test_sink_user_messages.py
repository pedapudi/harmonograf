"""Tests for the ADK ``on_user_message_callback`` integration that
forwards verbatim user-authored messages to harmonograf as
``UserMessageReceived`` envelopes.

Closes the long-standing UX gap where operator turns ("forget X. tell
me about Y.") were invisible across every harmonograf view (Sessions,
Gantt, Trajectory, Graph). The plugin now extracts the verbatim text
from the ADK ``types.Content`` user message, builds a
``harmonograf.v1.UserMessageReceived`` proto, and pushes via the
transport's dedicated ``TelemetryUp.user_message`` slot.

Coverage parallels ``test_sink_refine_events.py``:

* Plain-text user messages translate into protos with content + author
  populated; the plugin emits exactly one envelope per callback.
* Multi-part messages (text + non-text parts) carry only the text,
  newline-joined.
* Empty / textless messages are silently dropped.
* Mid-turn detection: a message that arrives while an invocation span
  is open sets ``mid_turn=True``; fresh top-level turns do not.
* Sequence counter is monotonic per plugin instance.
* Transport routing materializes the envelope on the right
  ``TelemetryUp`` oneof slot, and ``_session_id_of_envelope`` reads
  the proto's session_id for lazy-Hello.
* Dedup: a duplicate-installed plugin instance silently drops the
  callback (mirrors the existing dedup contract).
"""

from __future__ import annotations

from typing import Any, List

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


# --- ADK-shaped fakes (same minimal shapes as test_telemetry_plugin_dedup) ---


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class _PluginManager:
    def __init__(self, plugins: list[Any]) -> None:
        self.plugins = plugins


class _InvocationContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        plugins: list[Any],
        user_id: str = "alice",
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = _Agent("root-agent")
        self.plugin_manager = _PluginManager(plugins)
        self.user_id = user_id


class _Part:
    def __init__(self, text: str | None = None) -> None:
        self.text = text


class _Content:
    def __init__(self, parts: list[_Part]) -> None:
        self.parts = parts


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def made() -> List[FakeTransport]:
    return []


@pytest.fixture
def client(made: List[FakeTransport]) -> Client:
    return Client(
        name="research",
        agent_id="presentation-orchestrated-abc",
        session_id="sess-user-msg",
        framework="CUSTOM",
        buffer_size=32,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


# --- Tests ------------------------------------------------------------------


class TestUserMessageEmission:
    @pytest.mark.asyncio
    async def test_plain_text_message_translated_to_proto(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        ctx = _InvocationContext("inv-1", "sess-user-msg", [plugin])
        msg = _Content(
            [_Part(text="forget solar panels. tell me about solar flares.")]
        )
        await plugin.on_user_message_callback(
            invocation_context=ctx, user_message=msg
        )
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.USER_MESSAGE
        assert (
            env.payload.content
            == "forget solar panels. tell me about solar flares."
        )
        assert env.payload.author == "alice"
        assert env.payload.session_id == "sess-user-msg"
        assert env.payload.sequence == 1
        # Fresh top-level turn ⇒ mid_turn stays false (no invocation
        # span is open at on_user_message_callback time).
        assert env.payload.mid_turn is False
        assert env.payload.invocation_id == ""

    @pytest.mark.asyncio
    async def test_multi_part_message_text_only_concatenated(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        """Multi-part user messages (e.g. text + image) carry text-only
        joined with newlines so the marker still surfaces the
        operator's words even when the message has non-text parts."""
        ctx = _InvocationContext("inv-2", "sess-user-msg", [plugin])
        msg = _Content(
            [
                _Part(text="line 1"),
                _Part(text=None),  # image part — no text
                _Part(text="line 2"),
            ]
        )
        await plugin.on_user_message_callback(
            invocation_context=ctx, user_message=msg
        )
        env = _drain(client)[0]
        assert env.payload.content == "line 1\nline 2"

    @pytest.mark.asyncio
    async def test_empty_message_dropped_silently(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        """A message with no extractable text emits nothing — there's
        no point surfacing an empty marker on the timeline."""
        ctx = _InvocationContext("inv-3", "sess-user-msg", [plugin])
        await plugin.on_user_message_callback(
            invocation_context=ctx, user_message=_Content([])
        )
        await plugin.on_user_message_callback(
            invocation_context=ctx,
            user_message=_Content([_Part(text=None)]),
        )
        await plugin.on_user_message_callback(
            invocation_context=ctx, user_message=_Content([_Part(text="")])
        )
        assert _drain(client) == []

    @pytest.mark.asyncio
    async def test_mid_turn_flag_set_when_invocation_open(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        """Messages that arrive while an INVOCATION span is open
        carry ``mid_turn=True``. The plugin uses
        ``_invocation_spans`` membership as the discriminator."""
        ctx = _InvocationContext("inv-4", "sess-user-msg", [plugin])
        # Open an invocation so _invocation_spans["inv-4"] is set.
        await plugin.before_run_callback(invocation_context=ctx)
        await plugin.on_user_message_callback(
            invocation_context=ctx,
            user_message=_Content([_Part(text="interject!")]),
        )
        envs = _drain(client)
        # before_run emitted a SPAN_START + the user message envelope.
        user_envs = [e for e in envs if e.kind is EnvelopeKind.USER_MESSAGE]
        assert len(user_envs) == 1
        env = user_envs[0]
        assert env.payload.mid_turn is True
        assert env.payload.invocation_id == "inv-4"

    @pytest.mark.asyncio
    async def test_sequence_monotonic_per_plugin(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        ctx = _InvocationContext("inv-5", "sess-user-msg", [plugin])
        for i in range(3):
            await plugin.on_user_message_callback(
                invocation_context=ctx,
                user_message=_Content([_Part(text=f"msg {i}")]),
            )
        envs = _drain(client)
        seqs = [e.payload.sequence for e in envs]
        assert seqs == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_default_author_when_user_id_absent(
        self,
        client: Client,
        plugin: HarmonografTelemetryPlugin,
    ) -> None:
        """Falls back to ``"user"`` when ADK doesn't supply a
        user_id."""
        ctx = _InvocationContext("inv-6", "sess-user-msg", [plugin], user_id="")
        await plugin.on_user_message_callback(
            invocation_context=ctx,
            user_message=_Content([_Part(text="hi")]),
        )
        env = _drain(client)[0]
        assert env.payload.author == "user"

    @pytest.mark.asyncio
    async def test_dict_shaped_message_tolerated(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        """Some test shims pass ``types.Content`` as a plain dict.
        The text extractor falls back through the dict path so the
        marker still surfaces."""
        ctx = _InvocationContext("inv-7", "sess-user-msg", [plugin])
        await plugin.on_user_message_callback(
            invocation_context=ctx,
            user_message={"parts": [{"text": "from dict"}]},
        )
        env = _drain(client)[0]
        assert env.payload.content == "from dict"


class TestDuplicateInstall:
    @pytest.mark.asyncio
    async def test_duplicate_plugin_skips_callback(
        self, client: Client
    ) -> None:
        """A plugin instance that's been marked as a duplicate must
        not emit its own user-message envelope; the earlier instance
        is the authoritative emitter."""
        first = HarmonografTelemetryPlugin(client)
        second = HarmonografTelemetryPlugin(client)
        ctx = _InvocationContext("inv-d", "sess-user-msg", [first, second])
        # Trigger dedup detection on `second`.
        await first.before_run_callback(invocation_context=ctx)
        await second.before_run_callback(invocation_context=ctx)
        # Drain the spans before evaluating user-message behaviour.
        _drain(client)
        await second.on_user_message_callback(
            invocation_context=ctx,
            user_message=_Content([_Part(text="suppressed")]),
        )
        envs = _drain(client)
        user_envs = [e for e in envs if e.kind is EnvelopeKind.USER_MESSAGE]
        assert user_envs == []


class TestTransportMaterialization:
    @pytest.mark.asyncio
    async def test_round_trip_through_envelope_to_up(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        ctx = _InvocationContext("inv-rt", "sess-user-msg", [plugin])
        await plugin.on_user_message_callback(
            invocation_context=ctx,
            user_message=_Content([_Part(text="hello")]),
        )
        env = _drain(client)[0]
        from harmonograf_client.pb import telemetry_pb2

        up = telemetry_pb2.TelemetryUp(user_message=env.payload)
        assert up.WhichOneof("msg") == "user_message"
        assert up.user_message.content == "hello"

    @pytest.mark.asyncio
    async def test_session_id_routed_for_lazy_hello(
        self, plugin: HarmonografTelemetryPlugin, client: Client
    ) -> None:
        from harmonograf_client.transport import _session_id_of_envelope

        ctx = _InvocationContext("inv-lh", "sess-user-msg", [plugin])
        await plugin.on_user_message_callback(
            invocation_context=ctx,
            user_message=_Content([_Part(text="hi")]),
        )
        env = _drain(client)[0]
        assert _session_id_of_envelope(env) == "sess-user-msg"
