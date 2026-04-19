"""Shared test helpers for the harmonograf_client test suite.

Only used by the new comprehensive test files — legacy tests keep their
inline fakes untouched.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional


@dataclasses.dataclass
class FakeEnqueuedPayload:
    digest: str
    data: bytes
    mime: str


class FakeTransport:
    """Duck-typed stand-in for :class:`harmonograf_client.transport.Transport`.

    The real transport spins up a daemon thread and an asyncio loop; for
    unit tests against :class:`Client` we just need something that
    records calls, captures the ``progress_fn``, and can feed control
    events back through registered handlers.
    """

    def __init__(
        self,
        *,
        events: Any,
        payloads: Any,
        agent_id: str,
        session_id: str,
        name: str,
        framework: str,
        framework_version: str,
        capabilities: list[str],
        metadata: Optional[dict[str, str]] = None,
        session_title: str = "",
        config: Any = None,
        auth_token: Optional[str] = None,
        progress_fn: Optional[Callable[[], tuple[int, str]]] = None,
        context_window_fn: Optional[Callable[[], tuple[int, int]]] = None,
    ) -> None:
        self.events = events
        self.payloads = payloads
        self.agent_id = agent_id
        self.session_id = session_id
        self.name = name
        self.framework = framework
        self.framework_version = framework_version
        self.capabilities = list(capabilities)
        self.metadata = dict(metadata or {})
        self.session_title = session_title
        self.config = config
        self.auth_token = auth_token
        self.progress_fn = progress_fn
        self.context_window_fn = context_window_fn

        self.started = False
        self.shutdown_called = False
        self.notify_count = 0
        self.enqueued: list[FakeEnqueuedPayload] = []
        self.handlers: dict[str, Callable[[Any], Any]] = {}
        self.assigned_session_id: str = session_id
        self.payload_accept = True
        self.control_forward: Optional[Callable[[Any], None]] = None
        # (control_id, result, detail) tuples pushed via send_control_ack.
        self.sent_acks: list[tuple[str, str, str]] = []

    # --- public surface Client depends on -----------------------------

    def start(self) -> None:
        self.started = True

    def notify(self) -> None:
        self.notify_count += 1

    def shutdown(self, timeout: float = 5.0) -> None:
        self.shutdown_called = True

    def enqueue_payload(self, digest: str, data: bytes, mime: str) -> bool:
        self.enqueued.append(FakeEnqueuedPayload(digest, data, mime))
        return self.payload_accept

    def register_control_handler(self, kind: str, cb: Callable[[Any], Any]) -> None:
        self.handlers[kind] = cb

    def set_control_forward(self, fn: Optional[Callable[[Any], None]]) -> None:
        self.control_forward = fn

    def send_control_ack(
        self, control_id: str, result: str, detail: str = ""
    ) -> None:
        self.sent_acks.append((control_id, result, detail))

    # Bridge tests simulate a ControlEvent arriving on the control stream
    # by calling this directly. Matches the real transport's call path —
    # set_control_forward's callable receives a ControlEvent proto.
    def deliver_control_event(self, event: Any) -> None:
        fwd = self.control_forward
        if fwd is not None:
            fwd(event)


def make_factory(store: list[FakeTransport]) -> Callable[..., FakeTransport]:
    """Returns a factory that appends each constructed FakeTransport to ``store``."""

    def factory(**kwargs: Any) -> FakeTransport:
        t = FakeTransport(**kwargs)
        store.append(t)
        return t

    return factory
