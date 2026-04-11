"""Top-level Client handle — SKELETON.

Glues identity, buffer, and transport behind a small public API. The
non-blocking contract is enforced here: every ``emit_*`` method returns
without awaiting IO.

Finalized after task #2 lands.
"""

from __future__ import annotations


class Client:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "client.py is blocked on task #2 (proto codegen)."
        )
