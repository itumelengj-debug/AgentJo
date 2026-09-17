"""Persistent long-term memory backed by SQLite.

Three stores:
  - memories : durable facts, preferences, and standing instructions
  - skills   : user-taught procedures ("when X happens, do Y")
  - messages : an append-only conversation log (audit trail)

Search uses SQLite's built-in FTS5 full-text index with BM25 ranking,
falling back to LIKE-based keyword matching if FTS5 is unavailable.
No external services — everything lives in one local file.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    source      TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL,
    instructions TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    times_used   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playbooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL UNIQUE,
    trigger     TEXT NOT NULL,
    steps       TEXT NOT NULL,
    source_task INTEGER,
    uses        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    context    TEXT NOT NULL,
    lesson     TEXT NOT NULL,
    source     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,        -- command | write_dir
    pattern    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, pattern)
);

CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',   -- active | completed | abandoned
    summary    TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|in_progress|done|failed|skipped
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    rating     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    embedding   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    spec        TEXT NOT NULL,
    engine      TEXT NOT NULL DEFAULT 'Auto',
    full_access INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    next_run    REAL,
    last_run    REAL,
    last_status TEXT,
    last_summary TEXT,
    created_at  TEXT NOT NULL
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _age_seconds(ts: str) -> float:
    """Seconds since a stored '%Y-%m-%d %H:%M:%S' UTC timestamp (large if unknown)."""
    try:
        then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
    except Exception:
        return 1e12


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation/whitespace — used for duplicate detection."""
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


# Words in almost every memory; including them would make unrelated facts look
# similar. "user"/"users" are dropped because most memories read "User ...".
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "and", "or", "but", "with", "as", "by",
    "from", "that", "this", "it", "its", "their", "they", "them", "has", "have",
    "had", "do", "does", "did", "will", "would", "can", "could", "should", "i",
    "you", "he", "she", "we", "my", "your", "not", "also", "who", "which",
    "user", "users", "s", "re", "m", "ll", "ve", "currently", "now",
}


def _token_set(text: str) -> set:
    """Content words of a memory, for near-duplicate comparison."""
    return {w for w in _normalize(text).split() if len(w) > 1 and w not in _STOP}


def _source_rank(src: str) -> int:
    """Higher = keep preferentially. Manual/user memories outrank auto-learned."""
    return {"user": 2, "manual": 2}.get((src or "").lower(), 1)


class MemoryStore:
    def __init__(self, db_path: Path | None = None, check_same_thread: bool = True):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path),
                                    check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Tolerate concurrent access from parallel sessions (the web UI runs
        # turns in worker threads): wait briefly on a lock instead of erroring,
        # and use WAL so reads and a write can proceed at once.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(_BASE_SCHEMA)
        try:
            self.conn.executescript(_FTS_SCHEMA)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False  # rare; fall back to LIKE search
        try:  # migrate pre-rating databases in place
            self.conn.execute(
                "ALTER TABLE messages ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        for _alter in (
            "ALTER TABLE schedules ADD COLUMN action TEXT NOT NULL DEFAULT 'prompt'",
            "ALTER TABLE schedules ADD COLUMN payload TEXT",
        ):
            try:
                self.conn.execute(_alter)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def ensure_schema(self) -> None:
        """Re-apply the (idempotent) schema and migrations. Called after a
        restore so an older snapshot gains any tables/columns added since."""
        self.conn.executescript(_BASE_SCHEMA)
        try:
            self.conn.executescript(_FTS_SCHEMA)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False
        try:
            self.conn.execute(
                "ALTER TABLE messages ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for _alter in (
            "ALTER TABLE schedules ADD COLUMN action TEXT NOT NULL DEFAULT 'prompt'",
            "ALTER TABLE schedules ADD COLUMN payload TEXT",
        ):
            try:
                self.conn.execute(_alter)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Memories
    # ------------------------------------------------------------------ #
    def add_memory(self, content: str, category: str = "general",
                   source: str = "user") -> int | None:
        """Store a memory. Returns its id, or None if it's a duplicate."""
        content = " ".join(content.split()).strip()[:2000]
        if len(content) < 4:
            return None
        norm = _normalize(content)
        for row in self.conn.execute("SELECT content FROM memories"):
            if _normalize(row["content"]) == norm:
                return None  # exact duplicate (ignoring case/punctuation)
        cur = self.conn.execute(
            "INSERT INTO memories (content, category, source, created_at) "
            "VALUES (?, ?, ?, ?)",
            (content, category.strip().lower() or "general", source, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def search_memories(self, query: str, limit: int = 8) -> list[dict]:
        """Keyword search, best matches first."""
        words = [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2]
        if not words:
            return []
        rows: list[sqlite3.Row] = []
        if self.fts_enabled:
            fts_query = " OR ".join(dict.fromkeys(words[:16]))  # dedupe, cap
            try:
                rows = self.conn.execute(
                    "SELECT m.id, m.content, m.category, m.created_at, "
                    "       bm25(memories_fts) AS rank "
                    "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:  # FTS unavailable or no hits — LIKE fallback
            scored: dict[int, int] = {}
            for w in set(words[:16]):
                for r in self.conn.execute(
                    "SELECT id FROM memories WHERE content LIKE ? LIMIT 50",
                    (f"%{w}%",),
                ):
                    scored[r["id"]] = scored.get(r["id"], 0) + 1
            if scored:
                top_ids = sorted(scored, key=scored.get, reverse=True)[:limit]
                placeholders = ",".join("?" * len(top_ids))
                rows = self.conn.execute(
                    f"SELECT id, content, category, created_at FROM memories "
                    f"WHERE id IN ({placeholders})", top_ids,
                ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(
                f"UPDATE memories SET access_count = access_count + 1 "
                f"WHERE id IN ({placeholders})", ids,
            )
            self.conn.commit()
        out = [dict(r) for r in rows]
        # Semantic upgrade: when a local embedder exists, rerank by meaning —
        # and if keywords found nothing, let the embedder see a recent slice of
        # the whole store so a fully reworded query can still hit (same
        # treatment playbooks/lessons get). Without an embedder, behaviour is
        # byte-identical to the keyword ranking above.
        try:
            from . import semantic
            pool = out if out else [dict(r) for r in self.conn.execute(
                "SELECT id, content, category, created_at FROM memories "
                "ORDER BY access_count DESC, id DESC LIMIT 32")]
            sem = semantic.rerank(query, pool, lambda d: d["content"],
                                  limit=limit)
            if sem is not None:
                return sem
        except Exception:
            pass
        return out

    def recent_memories(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content, category, created_at FROM memories "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_memories(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content, category, source, created_at, access_count "
            "FROM memories ORDER BY id",
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_memories(self, ids) -> int:
        """Delete many memories at once. Returns how many were removed."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return cur.rowcount

    def find_similar_groups(self, threshold: float | None = None,
                            sim_threshold: float | None = None,
                            use_embeddings: bool = True,
                            max_scan: int = 600) -> list[dict]:
        """Cluster near-duplicate memories. Returns a list of groups, each
        {"keep": <memory>, "remove": [<memory>, ...]}, most-duplicated first.
        Exact (normalised) duplicates are already blocked on insert, so this
        targets reworded or overlapping facts.

        Two memories are grouped when their content words overlap a lot
        (Jaccard >= threshold) or one is almost contained in the other. When a
        local embedding model is available, paraphrases that share few words
        ("prefers Python for models" vs "likes Python for ML") are also grouped
        by cosine similarity (>= sim_threshold); without it, lexical-only."""
        mems = self.all_memories()
        if len(mems) > max_scan:
            mems = mems[-max_scan:]                 # recent window if huge
        if threshold is None:
            threshold = getattr(config, "MEMORY_DEDUP_JACCARD", 0.5)
        n = len(mems)
        toks = [_token_set(m["content"]) for m in mems]
        parent = list(range(n))

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            ti = toks[i]
            if not ti:
                continue
            for j in range(i + 1, n):
                tj = toks[j]
                if not tj:
                    continue
                inter = len(ti & tj)
                if not inter:
                    continue
                jacc = inter / len(ti | tj)
                contain = inter / min(len(ti), len(tj))
                if jacc >= threshold or contain >= 0.9:
                    union(i, j)

        # Optional semantic pass: catch paraphrases lexical overlap misses.
        if use_embeddings and n > 1:
            sim = sim_threshold if sim_threshold is not None else getattr(
                config, "MEMORY_DEDUP_SIM", 0.80)
            try:
                from . import rag
                embs = rag.embed_texts([mm["content"] for mm in mems])
            except Exception:
                embs = None
            if embs and len(embs) == n:
                for i in range(n):
                    if not embs[i]:
                        continue
                    for j in range(i + 1, n):
                        if not embs[j]:
                            continue
                        if find(i) == find(j):
                            continue
                        if rag._cosine(embs[i], embs[j]) >= sim:
                            union(i, j)

        clusters: dict[int, list] = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(mems[i])

        groups = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            keep = max(members, key=lambda mm: (_source_rank(mm["source"]),
                                                len(mm["content"]), mm["id"]))
            remove = [mm for mm in members if mm["id"] != keep["id"]]
            groups.append({"keep": keep, "remove": remove})
        groups.sort(key=lambda g: len(g["remove"]), reverse=True)
        return groups

    def memory_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Skills (taught procedures)
    # ------------------------------------------------------------------ #
    def add_skill(self, name: str, description: str, instructions: str) -> bool:
        """Add or update a taught skill. Returns True if it replaced an existing one."""
        name = name.strip().lower().replace(" ", "-")[:60]
        existing = self.conn.execute(
            "SELECT id FROM skills WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE skills SET description = ?, instructions = ? WHERE name = ?",
                (description.strip(), instructions.strip(), name),
            )
        else:
            self.conn.execute(
                "INSERT INTO skills (name, description, instructions, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, description.strip(), instructions.strip(), _now()),
            )
        self.conn.commit()
        return existing is not None

    def get_skills(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT name, description, instructions, times_used, created_at "
            "FROM skills ORDER BY name",
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_skill(self, name: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM skills WHERE name = ?",
            (name.strip().lower().replace(" ", "-"),),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def skill_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Experience — playbooks distilled from completed tasks, and lessons
    # recorded from blocked/failed work. Written by agent/experience.py.
    # ------------------------------------------------------------------ #
    def add_playbook(self, title: str, trigger: str, steps: str,
                     source_task=None) -> int:
        """Insert or refresh a playbook (same title => newest steps win)."""
        row = self.conn.execute(
            "SELECT id FROM playbooks WHERE title = ?", (title,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE playbooks SET trigger=?, steps=?, source_task=? WHERE id=?",
                (trigger, steps, source_task, row["id"]))
            self.conn.commit()
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO playbooks (title, trigger, steps, source_task, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (title, trigger, steps, source_task, _now()))
        self.conn.commit()
        return cur.lastrowid

    def relevant_playbooks(self, query: str, limit: int = 2) -> list[dict]:
        from . import experience, semantic
        qw = experience.sig_words(query)
        if not qw:
            return []
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, title, trigger, steps, uses FROM playbooks "
            "ORDER BY uses DESC, id DESC LIMIT 24")]
        # Semantic first: a fully reworded request should still find the playbook,
        # so the embedder sees every candidate and cosine decides.
        sem = semantic.rerank(query, rows,
                              lambda d: f"{d['title']}\n{d['steps'][:400]}",
                              limit=limit)
        if sem is not None:
            return sem
        scored = [(experience.overlap(qw, f"{r['title']} {r['trigger']}"), r)
                  for r in rows]
        scored.sort(key=lambda x: (-x[0], -x[1]["uses"]))
        return [d for sc, d in scored
                if sc >= experience.MIN_OVERLAP][:max(1, limit)]

    def touch_playbook(self, pid: int) -> None:
        self.conn.execute("UPDATE playbooks SET uses = uses + 1 WHERE id = ?",
                          (pid,))
        self.conn.commit()

    def playbook_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM playbooks").fetchone()[0]

    def add_lesson(self, context: str, lesson: str, source: str = "") -> int:
        dup = self.conn.execute(
            "SELECT id FROM lessons WHERE lesson = ?", (lesson,)).fetchone()
        if dup:
            return dup["id"]
        cur = self.conn.execute(
            "INSERT INTO lessons (context, lesson, source, created_at) "
            "VALUES (?, ?, ?, ?)", (context, lesson, source, _now()))
        self.conn.commit()
        return cur.lastrowid

    def relevant_lessons(self, query: str, limit: int = 3) -> list[dict]:
        from . import experience, semantic
        qw = experience.sig_words(query)
        if not qw:
            return []
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, context, lesson, created_at FROM lessons "
            "ORDER BY id DESC LIMIT 24")]
        sem = semantic.rerank(query, rows,
                              lambda d: f"{d['context']}\n{d['lesson']}",
                              limit=limit)
        if sem is not None:
            return sem
        scored = [(experience.overlap(qw, f"{r['context']} {r['lesson']}"), r)
                  for r in rows]
        scored.sort(key=lambda x: -x[0])
        return [d for sc, d in scored
                if sc >= experience.MIN_OVERLAP][:max(1, limit)]

    def lesson_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]

    def session_message_count(self, session_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,)).fetchone()[0]

    def recent_session_messages(self, session_id: str, n: int = 12) -> list[dict]:
        """Last n messages of one conversation, oldest first (for issue reports)."""
        rows = self.conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, max(1, n))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_activity_before(self, exclude_session: str = "") -> str | None:
        """When the user was last here (latest message outside this session)."""
        row = self.conn.execute(
            "SELECT MAX(created_at) AS m FROM messages WHERE session_id != ?",
            (exclude_session or "",)).fetchone()
        return row["m"] if row and row["m"] else None

    def tasks_changed_since(self, ts: str, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, title, status, updated_at FROM tasks "
            "WHERE updated_at > ? ORDER BY updated_at DESC LIMIT ?",
            (ts, limit)).fetchall()
        return [dict(r) for r in rows]

    def search_messages(self, query: str, exclude_session: str = "",
                        limit: int = 3, scan: int = 2000) -> list[dict]:
        """Cross-conversation recall: significant-word match over the most recent
        `scan` chat messages (excluding the current conversation, whose history is
        already in context). Returns snippets, best match first."""
        from . import experience
        qw = experience.sig_words(query)
        if not qw:
            return []
        rows = self.conn.execute(
            "SELECT session_id, role, content, created_at FROM messages "
            "WHERE session_id != ? AND role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT ?", (exclude_session or "", scan)).fetchall()
        pool = []
        for r in rows:
            txt = r["content"] or ""
            if txt.startswith("[auto-resume]"):
                continue
            sc = experience.overlap(qw, txt)
            if sc >= 1:
                pool.append((sc, {"session_id": r["session_id"],
                                  "role": r["role"],
                                  "created_at": r["created_at"],
                                  "snippet": " ".join(txt.split())[:280]}))
        pool.sort(key=lambda x: -x[0])
        from . import semantic
        sem = semantic.rerank(query, [d for _, d in pool[:24]],
                              lambda d: d["snippet"], limit=limit * 3)
        ranked = (sem if sem is not None
                  else [d for sc, d in pool if sc >= experience.MIN_OVERLAP])
        out, seen = [], set()
        for d in ranked:
            key = (d["session_id"], d["snippet"][:80])
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
            if len(out) >= max(1, limit):
                break
        return out


    # ------------------------------------------------------------------ #
    # Permissions — persisted "always allow" rules for tiered approvals
    # ------------------------------------------------------------------ #
    def add_permission(self, kind: str, pattern: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO permissions (kind, pattern, created_at) "
            "VALUES (?, ?, ?)", (kind, pattern.strip(), _now()))
        self.conn.commit()
        return cur.rowcount > 0

    def list_permissions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, kind, pattern, created_at FROM permissions ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_permission(self, perm_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM permissions WHERE id = ?", (perm_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def command_permitted(self, command: str) -> str | None:
        """Return the matching stored rule, or None. Rules only apply to
        SIMPLE commands (no shell metacharacters) and match token-wise as a
        prefix — 'npm run' allows 'npm run build' but not 'npmx'."""
        import shlex
        if any(ch in command for ch in ";&|><`$(){}\n"):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not tokens:
            return None
        for row in self.conn.execute(
                "SELECT pattern FROM permissions WHERE kind = 'command'"):
            try:
                pt = shlex.split(row["pattern"])
            except ValueError:
                continue
            if pt and tokens[:len(pt)] == pt:
                return row["pattern"]
        return None

    def write_permitted(self, path) -> str | None:
        """Return the stored directory rule covering this path, or None."""
        from pathlib import Path as _P
        try:
            target = _P(path).expanduser().resolve()
        except OSError:
            return None
        for row in self.conn.execute(
                "SELECT pattern FROM permissions WHERE kind = 'write_dir'"):
            try:
                if target.is_relative_to(_P(row["pattern"])):
                    return row["pattern"]
            except (OSError, ValueError):
                continue
        return None

    # ------------------------------------------------------------------ #
    # Tasks — persistent multi-step plans (survive restarts, resumable)
    # ------------------------------------------------------------------ #
    def create_task(self, title: str, steps: list[str],
                    session_id: str = "") -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO tasks (title, status, session_id, created_at, updated_at) "
            "VALUES (?, 'active', ?, ?, ?)",
            (title.strip()[:200], session_id, now, now))
        task_id = cur.lastrowid
        for i, desc in enumerate(steps, 1):
            self.conn.execute(
                "INSERT INTO task_steps (task_id, seq, description, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, i, str(desc).strip()[:500], now))
        self.conn.commit()
        return task_id

    def get_task(self, task_id: int) -> dict | None:
        t = self.conn.execute("SELECT * FROM tasks WHERE id = ?",
                              (task_id,)).fetchone()
        if not t:
            return None
        steps = self.conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY seq",
            (task_id,)).fetchall()
        task = dict(t)
        task["steps"] = [dict(r) for r in steps]
        return task

    def active_tasks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status = 'active' ORDER BY id").fetchall()
        return [self.get_task(r["id"]) for r in rows]

    def list_tasks(self, status: str = "all", limit: int = 20) -> list[dict]:
        if status == "all":
            rows = self.conn.execute(
                "SELECT id FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.conn.execute(
                "SELECT id FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit))
        return [self.get_task(r["id"]) for r in rows.fetchall()]

    def update_step(self, task_id: int, seq: int, status: str,
                    note: str = "") -> bool:
        now = _now()
        cur = self.conn.execute(
            "UPDATE task_steps SET status = ?, note = ?, updated_at = ? "
            "WHERE task_id = ? AND seq = ?",
            (status, note.strip()[:500], now, task_id, seq))
        if cur.rowcount:
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                              (now, task_id))
        self.conn.commit()
        return cur.rowcount > 0

    def finish_task(self, task_id: int, summary: str,
                    status: str = "completed") -> bool:
        cur = self.conn.execute(
            "UPDATE tasks SET status = ?, summary = ?, updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (status, summary.strip()[:1000], _now(), task_id))
        self.conn.commit()
        return cur.rowcount > 0

    def reset_task_plan(self, task_id: int) -> bool:
        """Restart a task in place: clear step progress but KEEP the task id and
        its accumulated step notes as history. Fixes the 'start over spawns a new
        task and orphans the old context' failure pattern."""
        now = _now()
        t = self.get_task(task_id)
        if not t:
            return False
        for s in t["steps"]:
            old = (s.get("note") or "").strip()
            keep = (f"(prev: {old[:160]})" if old else "")
            self.conn.execute(
                "UPDATE task_steps SET status = 'pending', note = ?, updated_at = ? "
                "WHERE task_id = ? AND seq = ?",
                (keep, now, task_id, s["seq"]))
        self.conn.execute(
            "UPDATE tasks SET status = 'active', summary = '', updated_at = ? "
            "WHERE id = ?", (now, task_id))
        self.conn.commit()
        return True

    def find_active_task(self, title: str) -> dict | None:
        """Find an existing active task whose title overlaps the given one, so a
        restart reuses it instead of creating a duplicate."""
        want = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
        if not want:
            return None
        best, best_score = None, 0.0
        for t in self.active_tasks():
            have = set(re.findall(r"[a-z0-9]+", (t["title"] or "").lower()))
            if not have:
                continue
            score = len(want & have) / max(1, min(len(want), len(have)))
            if score > best_score:
                best, best_score = t, score
        return best if best_score >= 0.5 else None

    def stuck_tasks(self, idle_minutes: int = 0) -> list[dict]:
        """Active tasks that still have unresolved steps and haven't moved in a
        while — the ones that silently stalled mid-plan."""
        out = []
        for t in self.active_tasks():
            unresolved = [s for s in t["steps"]
                          if s["status"] in ("pending", "in_progress", "blocked", "failed")]
            if not unresolved:
                continue
            if idle_minutes and _age_seconds(t.get("updated_at")) < idle_minutes * 60:
                continue
            t["next_step"] = next((s for s in t["steps"]
                                   if s["status"] in ("pending", "in_progress")), None)
            t["blocked_steps"] = [s for s in t["steps"] if s["status"] == "blocked"]
            out.append(t)
        return out

    # ------------------------------------------------------------------ #
    # Conversation log (audit trail)
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Conversation sessions (derived from the message log)
    # ------------------------------------------------------------------ #
    def list_sessions(self, limit: int = 30) -> list[dict]:
        """Recent conversations: id, message count, first/last timestamps, and
        a title taken from the first user message."""
        rows = self.conn.execute(
            "SELECT session_id, COUNT(*) AS n, MIN(created_at) AS first_ts, "
            "MAX(created_at) AS last_ts FROM messages "
            "GROUP BY session_id ORDER BY last_ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            t = self.conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND "
                "role = 'user' ORDER BY id LIMIT 1", (r["session_id"],)
            ).fetchone()
            title = " ".join((t["content"] if t else "(no user message)").split())[:60]
            out.append({"session_id": r["session_id"], "n": r["n"],
                        "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                        "title": title})
        return out

    def get_transcript(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_sessions(self, prefix: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT session_id FROM messages WHERE session_id LIKE ?",
            (prefix + "%",)).fetchall()
        return [r["session_id"] for r in rows]

    def delete_session(self, session_id: str) -> int:
        cur = self.conn.execute("DELETE FROM messages WHERE session_id = ?",
                                (session_id,))
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ #
    # Routing feedback (learned escalations to Claude)
    # ------------------------------------------------------------------ #
    def add_routing_escalation(self, text: str, embedding: str | None = None) -> int | None:
        text = (text or "").strip()
        if not text:
            return None
        existing = self.conn.execute(
            "SELECT id FROM routing_feedback WHERE text = ?", (text,)).fetchone()
        if existing:                          # don't log the same prompt twice
            if embedding:                     # but backfill an embedding if we now have one
                self.conn.execute(
                    "UPDATE routing_feedback SET embedding = ? WHERE id = ?",
                    (embedding, existing["id"]))
                self.conn.commit()
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO routing_feedback (text, embedding, created_at) "
            "VALUES (?, ?, ?)", (text, embedding, _now()))
        self.conn.commit()
        return cur.lastrowid

    def get_routing_escalations(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, text, embedding, created_at FROM routing_feedback "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_routing_escalations(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM routing_feedback").fetchone()[0]

    def delete_routing_escalation(self, fid: int) -> bool:
        cur = self.conn.execute("DELETE FROM routing_feedback WHERE id = ?", (fid,))
        self.conn.commit()
        return cur.rowcount > 0

    def clear_routing_escalations(self) -> int:
        n = self.count_routing_escalations()
        self.conn.execute("DELETE FROM routing_feedback")
        self.conn.commit()
        return n

    # ------------------------------------------------------------------ #
    # Scheduled tasks
    # ------------------------------------------------------------------ #
    def create_schedule(self, name: str, prompt: str, spec_json: str,
                        engine: str = "Auto", full_access: bool = False,
                        next_run: float | None = None,
                        action: str = "prompt", payload: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO schedules (name, prompt, spec, engine, full_access, "
            "enabled, next_run, created_at, action, payload) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (name.strip(), prompt.strip(), spec_json, engine,
             int(bool(full_access)), next_run, _now(), action, payload))
        self.conn.commit()
        return cur.lastrowid

    def list_schedules(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM schedules ORDER BY id").fetchall()]

    def get_schedule(self, sched_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM schedules WHERE id = ?",
                              (sched_id,)).fetchone()
        return dict(r) if r else None

    def set_schedule_enabled(self, sched_id: int, enabled: bool,
                             next_run: float | None = None) -> bool:
        cur = self.conn.execute(
            "UPDATE schedules SET enabled = ?, next_run = ? WHERE id = ?",
            (int(bool(enabled)), next_run, sched_id))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_schedule(self, sched_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def due_schedules(self, now_ts: float) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL "
            "AND next_run <= ?", (now_ts,)).fetchall()]

    def schedule_ran(self, sched_id: int, last_run: float,
                     next_run: float | None, status: str, summary: str) -> None:
        self.conn.execute(
            "UPDATE schedules SET last_run = ?, next_run = ?, last_status = ?, "
            "last_summary = ? WHERE id = ?",
            (last_run, next_run, status, summary[:400], sched_id))
        self.conn.commit()

    def log_message(self, session_id: str, role: str, content: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, content[:20_000], _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def rate_message(self, message_id: int, rating: int) -> None:
        """+1 / -1 from /good and /bad — used to curate fine-tuning data."""
        self.conn.execute("UPDATE messages SET rating = ? WHERE id = ?",
                          (rating, message_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


_STEP_MARK = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]",
              "failed": "[!]", "skipped": "[~]", "blocked": "[B]"}


def format_task(task: dict) -> str:
    """Render a task plan as a compact checklist for prompts and the CLI."""
    resolved = sum(1 for s in task["steps"]
                   if s["status"] in ("done", "skipped"))
    lines = [f"Task #{task['id']}: {task['title']}  "
             f"({task['status']}, {resolved}/{len(task['steps'])} steps resolved)"]
    for s in task["steps"]:
        mark = _STEP_MARK.get(s["status"], "[?]")
        line = f"  {s['seq']}. {mark} {s['description']}"
        if s["note"]:
            line += f"  — {s['note']}"
        lines.append(line)
    if task.get("summary"):
        lines.append(f"  outcome: {task['summary']}")
    return "\n".join(lines)
