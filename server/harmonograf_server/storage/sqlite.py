"""SQLite-backed store using aiosqlite.

Span metadata lives in tables. Payload bytes are content-addressed and stored
on disk under ``data/payloads/{digest[:2]}/{digest}`` so the database stays
small and dedup is structural (a sha256 collision is the only way to clobber).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from harmonograf_server.storage.base import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Capability,
    Framework,
    LinkRelation,
    PayloadMeta,
    PayloadRecord,
    Session,
    SessionStatus,
    Span,
    SpanKind,
    SpanLink,
    SpanStatus,
    Stats,
    Store,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    framework TEXT NOT NULL,
    framework_version TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    metadata TEXT NOT NULL,
    connected_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_agents_session ON agents(session_id);

CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    parent_span_id TEXT,
    kind TEXT NOT NULL,
    kind_string TEXT,
    status TEXT NOT NULL,
    name TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL,
    attributes TEXT NOT NULL DEFAULT '{}',
    payload_digest TEXT,
    payload_mime TEXT NOT NULL DEFAULT '',
    payload_size INTEGER NOT NULL DEFAULT 0,
    payload_summary TEXT NOT NULL DEFAULT '',
    payload_role TEXT NOT NULL DEFAULT '',
    payload_evicted INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_sa_time
    ON spans(session_id, agent_id, start_time);
CREATE INDEX IF NOT EXISTS idx_spans_session_time
    ON spans(session_id, start_time);
CREATE INDEX IF NOT EXISTS idx_spans_payload
    ON spans(payload_digest);

CREATE TABLE IF NOT EXISTS span_links (
    span_id TEXT NOT NULL,
    target_span_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY (span_id, target_span_id, relation)
);

CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    target_span_id TEXT,
    target_agent_id TEXT,
    target_time_start REAL,
    target_time_end REAL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_annotations_session ON annotations(session_id);
CREATE INDEX IF NOT EXISTS idx_annotations_span ON annotations(target_span_id);

CREATE TABLE IF NOT EXISTS payloads (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL
);
"""


class SqliteStore(Store):
    def __init__(self, db_path: str | os.PathLike, payload_dir: Optional[str | os.PathLike] = None) -> None:
        self.db_path = Path(db_path)
        if payload_dir is None:
            payload_dir = self.db_path.parent / "payloads"
        self.payload_dir = Path(payload_dir)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        # Enable WAL and set busy_timeout *before* schema so concurrent
        # processes (e.g. a stale server left running) don't immediately
        # deadlock on startup writes.
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._db.executescript(SCHEMA)
        # Backfill payload_* columns on pre-existing DBs created before task #7.
        async with self._db.execute("PRAGMA table_info(spans)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        for name, ddl in (
            ("payload_mime", "ALTER TABLE spans ADD COLUMN payload_mime TEXT NOT NULL DEFAULT ''"),
            ("payload_size", "ALTER TABLE spans ADD COLUMN payload_size INTEGER NOT NULL DEFAULT 0"),
            ("payload_summary", "ALTER TABLE spans ADD COLUMN payload_summary TEXT NOT NULL DEFAULT ''"),
            ("payload_role", "ALTER TABLE spans ADD COLUMN payload_role TEXT NOT NULL DEFAULT ''"),
            ("payload_evicted", "ALTER TABLE spans ADD COLUMN payload_evicted INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in cols:
                await self._db.execute(ddl)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SqliteStore not started; call start() first")
        return self._db

    # sessions ------------------------------------------------------------
    async def create_session(self, session: Session) -> Session:
        async with self._lock:
            existing = await self._fetch_session(session.id)
            if existing is not None:
                return existing
            await self.db.execute(
                "INSERT INTO sessions (id, title, created_at, ended_at, status, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.title,
                    session.created_at,
                    session.ended_at,
                    session.status.value,
                    json.dumps(session.metadata),
                ),
            )
            await self.db.commit()
            return await self._fetch_session(session.id)  # type: ignore[return-value]

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            return await self._fetch_session(session_id)

    async def _fetch_session(self, session_id: str) -> Optional[Session]:
        async with self.db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
        agent_ids = await self._agent_ids_for_session(session_id)
        return Session(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            ended_at=row["ended_at"],
            status=SessionStatus(row["status"]),
            agent_ids=agent_ids,
            metadata=json.loads(row["metadata"]),
        )

    async def _agent_ids_for_session(self, session_id: str) -> list[str]:
        async with self.db.execute(
            "SELECT id FROM agents WHERE session_id = ? ORDER BY connected_at",
            (session_id,),
        ) as cur:
            return [r["id"] for r in await cur.fetchall()]

    async def list_sessions(
        self,
        status: Optional[SessionStatus] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        async with self._lock:
            query = "SELECT id FROM sessions"
            args: list[Any] = []
            if status is not None:
                query += " WHERE status = ?"
                args.append(status.value)
            query += " ORDER BY created_at DESC"
            if limit is not None:
                query += " LIMIT ?"
                args.append(limit)
            async with self.db.execute(query, args) as cur:
                rows = await cur.fetchall()
            out = []
            for r in rows:
                s = await self._fetch_session(r["id"])
                if s:
                    out.append(s)
            return out

    async def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        ended_at: Optional[float] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Optional[Session]:
        async with self._lock:
            current = await self._fetch_session(session_id)
            if current is None:
                return None
            new_title = title if title is not None else current.title
            new_status = status.value if status is not None else current.status.value
            new_ended = ended_at if ended_at is not None else current.ended_at
            new_meta = dict(current.metadata)
            if metadata:
                new_meta.update(metadata)
            await self.db.execute(
                "UPDATE sessions SET title=?, status=?, ended_at=?, metadata=? WHERE id=?",
                (
                    new_title,
                    new_status,
                    new_ended,
                    json.dumps(new_meta),
                    session_id,
                ),
            )
            await self.db.commit()
            return await self._fetch_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        async with self._lock:
            async with self.db.execute(
                "SELECT payload_digest FROM spans WHERE session_id = ? AND payload_digest IS NOT NULL",
                (session_id,),
            ) as cur:
                digests = [r["payload_digest"] for r in await cur.fetchall()]
            async with self.db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ) as cur:
                exists = await cur.fetchone() is not None
            if not exists:
                return False
            await self.db.execute(
                "DELETE FROM span_links WHERE span_id IN (SELECT id FROM spans WHERE session_id = ?)",
                (session_id,),
            )
            await self.db.execute("DELETE FROM spans WHERE session_id = ?", (session_id,))
            await self.db.execute("DELETE FROM annotations WHERE session_id = ?", (session_id,))
            await self.db.execute("DELETE FROM agents WHERE session_id = ?", (session_id,))
            await self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await self.db.commit()
            for d in set(digests):
                await self._maybe_gc_payload(d)
            return True

    async def _maybe_gc_payload(self, digest: str) -> None:
        async with self.db.execute(
            "SELECT 1 FROM spans WHERE payload_digest = ? LIMIT 1", (digest,)
        ) as cur:
            still_used = await cur.fetchone() is not None
        if still_used:
            return
        async with self.db.execute(
            "SELECT path FROM payloads WHERE digest = ?", (digest,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        await self.db.execute("DELETE FROM payloads WHERE digest = ?", (digest,))
        await self.db.commit()

    # agents --------------------------------------------------------------
    async def register_agent(self, agent: Agent) -> Agent:
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO agents (id, session_id, name, framework, framework_version,
                                    capabilities, metadata, connected_at, last_heartbeat, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, session_id) DO UPDATE SET
                    name=excluded.name,
                    framework=excluded.framework,
                    framework_version=excluded.framework_version,
                    capabilities=excluded.capabilities,
                    metadata=excluded.metadata,
                    last_heartbeat=excluded.last_heartbeat,
                    status=excluded.status
                """,
                (
                    agent.id,
                    agent.session_id,
                    agent.name,
                    agent.framework.value,
                    agent.framework_version,
                    json.dumps([c.value for c in agent.capabilities]),
                    json.dumps(agent.metadata),
                    agent.connected_at,
                    agent.last_heartbeat,
                    agent.status.value,
                ),
            )
            await self.db.commit()
            return await self._fetch_agent(agent.session_id, agent.id)  # type: ignore[return-value]

    async def _fetch_agent(self, session_id: str, agent_id: str) -> Optional[Agent]:
        async with self.db.execute(
            "SELECT * FROM agents WHERE id=? AND session_id=?",
            (agent_id, session_id),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
        return Agent(
            id=row["id"],
            session_id=row["session_id"],
            name=row["name"],
            framework=Framework(row["framework"]),
            framework_version=row["framework_version"],
            capabilities=[Capability(c) for c in json.loads(row["capabilities"])],
            metadata=json.loads(row["metadata"]),
            connected_at=row["connected_at"],
            last_heartbeat=row["last_heartbeat"],
            status=AgentStatus(row["status"]),
        )

    async def get_agent(self, session_id: str, agent_id: str) -> Optional[Agent]:
        async with self._lock:
            return await self._fetch_agent(session_id, agent_id)

    async def list_agents_for_session(self, session_id: str) -> list[Agent]:
        async with self._lock:
            async with self.db.execute(
                "SELECT id FROM agents WHERE session_id=? ORDER BY connected_at",
                (session_id,),
            ) as cur:
                rows = await cur.fetchall()
            out = []
            for r in rows:
                a = await self._fetch_agent(session_id, r["id"])
                if a:
                    out.append(a)
            return out

    async def update_agent_status(
        self,
        session_id: str,
        agent_id: str,
        status: AgentStatus,
        last_heartbeat: Optional[float] = None,
    ) -> None:
        async with self._lock:
            if last_heartbeat is not None:
                await self.db.execute(
                    "UPDATE agents SET status=?, last_heartbeat=? WHERE id=? AND session_id=?",
                    (status.value, last_heartbeat, agent_id, session_id),
                )
            else:
                await self.db.execute(
                    "UPDATE agents SET status=? WHERE id=? AND session_id=?",
                    (status.value, agent_id, session_id),
                )
            await self.db.commit()

    # spans ---------------------------------------------------------------
    async def append_span(self, span: Span) -> Span:
        async with self._lock:
            async with self.db.execute(
                "SELECT 1 FROM spans WHERE id = ?", (span.id,)
            ) as cur:
                if await cur.fetchone() is not None:
                    existing = await self._fetch_span(span.id)
                    return existing  # type: ignore[return-value]
            await self.db.execute(
                """
                INSERT INTO spans (id, session_id, agent_id, parent_span_id, kind, kind_string,
                                   status, name, start_time, end_time, attributes, payload_digest,
                                   payload_mime, payload_size, payload_summary, payload_role, payload_evicted, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span.id,
                    span.session_id,
                    span.agent_id,
                    span.parent_span_id,
                    span.kind.value,
                    span.kind_string,
                    span.status.value,
                    span.name,
                    span.start_time,
                    span.end_time,
                    json.dumps(span.attributes),
                    span.payload_digest,
                    span.payload_mime,
                    span.payload_size,
                    span.payload_summary,
                    span.payload_role,
                    1 if span.payload_evicted else 0,
                    json.dumps(span.error) if span.error is not None else None,
                ),
            )
            for link in span.links:
                await self.db.execute(
                    "INSERT OR IGNORE INTO span_links (span_id, target_span_id, target_agent_id, relation) VALUES (?, ?, ?, ?)",
                    (span.id, link.target_span_id, link.target_agent_id, link.relation.value),
                )
            await self.db.commit()
            return await self._fetch_span(span.id)  # type: ignore[return-value]

    async def _fetch_span(self, span_id: str) -> Optional[Span]:
        async with self.db.execute(
            "SELECT * FROM spans WHERE id = ?", (span_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
        async with self.db.execute(
            "SELECT * FROM span_links WHERE span_id = ?", (span_id,)
        ) as cur:
            link_rows = await cur.fetchall()
        return Span(
            id=row["id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            parent_span_id=row["parent_span_id"],
            kind=SpanKind(row["kind"]),
            kind_string=row["kind_string"],
            status=SpanStatus(row["status"]),
            name=row["name"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            attributes=json.loads(row["attributes"]),
            payload_digest=row["payload_digest"],
            payload_mime=row["payload_mime"] or "",
            payload_size=row["payload_size"] or 0,
            payload_summary=row["payload_summary"] or "",
            payload_role=row["payload_role"] or "",
            payload_evicted=bool(row["payload_evicted"]),
            error=json.loads(row["error"]) if row["error"] else None,
            links=[
                SpanLink(
                    target_span_id=lr["target_span_id"],
                    target_agent_id=lr["target_agent_id"],
                    relation=LinkRelation(lr["relation"]),
                )
                for lr in link_rows
            ],
        )

    async def update_span(
        self,
        span_id: str,
        *,
        status: Optional[SpanStatus] = None,
        attributes: Optional[dict] = None,
        payload_digest: Optional[str] = None,
        payload_mime: Optional[str] = None,
        payload_size: Optional[int] = None,
        payload_summary: Optional[str] = None,
        payload_role: Optional[str] = None,
        payload_evicted: Optional[bool] = None,
        error: Optional[dict] = None,
    ) -> Optional[Span]:
        async with self._lock:
            current = await self._fetch_span(span_id)
            if current is None:
                return None
            new_status = status.value if status is not None else current.status.value
            new_attrs = dict(current.attributes)
            if attributes:
                new_attrs.update(attributes)
            new_payload = payload_digest if payload_digest is not None else current.payload_digest
            new_mime = payload_mime if payload_mime is not None else current.payload_mime
            new_size = payload_size if payload_size is not None else current.payload_size
            new_summary = payload_summary if payload_summary is not None else current.payload_summary
            new_role = payload_role if payload_role is not None else current.payload_role
            new_evicted = payload_evicted if payload_evicted is not None else current.payload_evicted
            new_error = error if error is not None else current.error
            await self.db.execute(
                """
                UPDATE spans SET status=?, attributes=?, payload_digest=?,
                                 payload_mime=?, payload_size=?, payload_summary=?,
                                 payload_role=?, payload_evicted=?, error=?
                WHERE id=?
                """,
                (
                    new_status,
                    json.dumps(new_attrs),
                    new_payload,
                    new_mime,
                    new_size,
                    new_summary,
                    new_role,
                    1 if new_evicted else 0,
                    json.dumps(new_error) if new_error is not None else None,
                    span_id,
                ),
            )
            await self.db.commit()
            return await self._fetch_span(span_id)

    async def end_span(
        self,
        span_id: str,
        end_time: float,
        status: SpanStatus,
        error: Optional[dict] = None,
    ) -> Optional[Span]:
        async with self._lock:
            current = await self._fetch_span(span_id)
            if current is None:
                return None
            new_error = error if error is not None else current.error
            await self.db.execute(
                "UPDATE spans SET end_time=?, status=?, error=? WHERE id=?",
                (
                    end_time,
                    status.value,
                    json.dumps(new_error) if new_error is not None else None,
                    span_id,
                ),
            )
            await self.db.commit()
            return await self._fetch_span(span_id)

    async def get_span(self, span_id: str) -> Optional[Span]:
        async with self._lock:
            return await self._fetch_span(span_id)

    async def get_spans(
        self,
        session_id: str,
        agent_id: Optional[str] = None,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[Span]:
        async with self._lock:
            query = "SELECT id FROM spans WHERE session_id = ?"
            args: list[Any] = [session_id]
            if agent_id is not None:
                query += " AND agent_id = ?"
                args.append(agent_id)
            if time_end is not None:
                query += " AND start_time <= ?"
                args.append(time_end)
            if time_start is not None:
                # Span overlaps the window if its end (or start, when open) >= window start.
                query += " AND COALESCE(end_time, start_time) >= ?"
                args.append(time_start)
            query += " ORDER BY start_time"
            if limit is not None:
                query += " LIMIT ?"
                args.append(limit)
            async with self.db.execute(query, args) as cur:
                ids = [r["id"] for r in await cur.fetchall()]
            out = []
            for sid in ids:
                sp = await self._fetch_span(sid)
                if sp:
                    out.append(sp)
            return out

    # annotations ---------------------------------------------------------
    async def put_annotation(self, annotation: Annotation) -> Annotation:
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO annotations (id, session_id, target_span_id, target_agent_id,
                                         target_time_start, target_time_end, kind, body,
                                         author, created_at, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    body=excluded.body,
                    delivered_at=excluded.delivered_at
                """,
                (
                    annotation.id,
                    annotation.session_id,
                    annotation.target.span_id,
                    annotation.target.agent_id,
                    annotation.target.time_start,
                    annotation.target.time_end,
                    annotation.kind.value,
                    annotation.body,
                    annotation.author,
                    annotation.created_at,
                    annotation.delivered_at,
                ),
            )
            await self.db.commit()
            return await self._fetch_annotation(annotation.id)  # type: ignore[return-value]

    async def _fetch_annotation(self, annotation_id: str) -> Optional[Annotation]:
        async with self.db.execute(
            "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
        return Annotation(
            id=row["id"],
            session_id=row["session_id"],
            target=AnnotationTarget(
                span_id=row["target_span_id"],
                agent_id=row["target_agent_id"],
                time_start=row["target_time_start"],
                time_end=row["target_time_end"],
            ),
            author=row["author"],
            created_at=row["created_at"],
            kind=AnnotationKind(row["kind"]),
            body=row["body"],
            delivered_at=row["delivered_at"],
        )

    async def list_annotations(
        self,
        session_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> list[Annotation]:
        async with self._lock:
            query = "SELECT id FROM annotations WHERE 1=1"
            args: list[Any] = []
            if session_id is not None:
                query += " AND session_id = ?"
                args.append(session_id)
            if span_id is not None:
                query += " AND target_span_id = ?"
                args.append(span_id)
            query += " ORDER BY created_at"
            async with self.db.execute(query, args) as cur:
                ids = [r["id"] for r in await cur.fetchall()]
            out = []
            for aid in ids:
                a = await self._fetch_annotation(aid)
                if a:
                    out.append(a)
            return out

    # payloads ------------------------------------------------------------
    def _payload_path(self, digest: str) -> Path:
        return self.payload_dir / digest[:2] / digest

    async def put_payload(
        self, digest: str, data: bytes, mime: str, summary: str = ""
    ) -> PayloadMeta:
        async with self._lock:
            async with self.db.execute(
                "SELECT digest, size, mime, summary FROM payloads WHERE digest = ?",
                (digest,),
            ) as cur:
                row = await cur.fetchone()
                if row is not None:
                    return PayloadMeta(
                        digest=row["digest"],
                        size=row["size"],
                        mime=row["mime"],
                        summary=row["summary"],
                    )
            path = self._payload_path(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.get_running_loop().run_in_executor(
                None, path.write_bytes, data
            )
            await self.db.execute(
                "INSERT INTO payloads (digest, size, mime, summary, path) VALUES (?, ?, ?, ?, ?)",
                (digest, len(data), mime, summary, str(path)),
            )
            await self.db.commit()
            return PayloadMeta(digest=digest, size=len(data), mime=mime, summary=summary)

    async def get_payload(self, digest: str) -> Optional[PayloadRecord]:
        async with self._lock:
            async with self.db.execute(
                "SELECT * FROM payloads WHERE digest = ?", (digest,)
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    return None
            try:
                data = await asyncio.get_running_loop().run_in_executor(
                    None, Path(row["path"]).read_bytes
                )
            except FileNotFoundError:
                return None
            return PayloadRecord(
                meta=PayloadMeta(
                    digest=row["digest"],
                    size=row["size"],
                    mime=row["mime"],
                    summary=row["summary"],
                ),
                bytes_=data,
            )

    async def has_payload(self, digest: str) -> bool:
        async with self._lock:
            async with self.db.execute(
                "SELECT 1 FROM payloads WHERE digest = ?", (digest,)
            ) as cur:
                return (await cur.fetchone()) is not None

    async def ping(self) -> bool:
        if self._db is None:
            return False
        try:
            async with self._lock:
                async with self.db.execute("SELECT 1") as cur:
                    return (await cur.fetchone()) is not None
        except Exception:
            return False

    async def gc_payloads(self) -> int:
        async with self._lock:
            async with self.db.execute(
                """
                SELECT p.digest, p.path
                FROM payloads p
                LEFT JOIN spans s ON s.payload_digest = p.digest
                WHERE s.id IS NULL
                """
            ) as cur:
                orphans = [(r["digest"], r["path"]) for r in await cur.fetchall()]
            for digest, path in orphans:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
                await self.db.execute(
                    "DELETE FROM payloads WHERE digest = ?", (digest,)
                )
            if orphans:
                await self.db.commit()
            return len(orphans)

    # stats ---------------------------------------------------------------
    async def stats(self) -> Stats:
        async with self._lock:
            async with self.db.execute("SELECT COUNT(*) AS n FROM sessions") as cur:
                session_count = (await cur.fetchone())["n"]
            async with self.db.execute("SELECT COUNT(*) AS n FROM agents") as cur:
                agent_count = (await cur.fetchone())["n"]
            async with self.db.execute("SELECT COUNT(*) AS n FROM spans") as cur:
                span_count = (await cur.fetchone())["n"]
            async with self.db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS s FROM payloads"
            ) as cur:
                row = await cur.fetchone()
                payload_count = row["n"]
                payload_bytes = row["s"]
            disk = 0
            try:
                disk += self.db_path.stat().st_size
            except OSError:
                pass
            if self.payload_dir.exists():
                for root, _dirs, files in os.walk(self.payload_dir):
                    for f in files:
                        try:
                            disk += (Path(root) / f).stat().st_size
                        except OSError:
                            pass
            return Stats(
                session_count=session_count,
                agent_count=agent_count,
                span_count=span_count,
                payload_count=payload_count,
                payload_bytes=payload_bytes,
                disk_usage_bytes=disk,
            )
