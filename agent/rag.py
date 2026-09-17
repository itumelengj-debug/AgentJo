"""Local document retrieval (RAG) for Agent Jo.

Point Agent Jo at a file or folder; this chunks the text, embeds each chunk with
a LOCAL Ollama embedding model (so documents never leave the machine), and
stores everything in SQLite. At query time it embeds the question and returns
the most similar chunks. If no embedding model is available, it falls back to
SQLite FTS5 keyword search, so retrieval still works (just less "semantic").

Everything here is local and dependency-light: plain-text formats need nothing
extra; PDF/DOCX use pypdf / python-docx only if those are installed.
"""

import json
import time as _time
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config

# File types we can read. Text types need no extra libraries.
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".text", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rb", ".rs", ".php", ".swift", ".kt", ".scala", ".sh",
    ".sql", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".html", ".htm", ".xml", ".tex",
}
DOC_EXTS = {".pdf", ".docx"}          # need optional libraries

# Chunks per embedding call. Ollama handles a few hundred comfortably, and
# each call carries fixed overhead — so one call per file was the worst
# possible shape for a folder of many small files.
EMBED_BATCH = int(os.environ.get("AGENT_EMBED_BATCH", "256"))

# How long one indexing call runs before stopping cleanly and reporting.
# A browser request times out long before 12,000 files are done, so the run
# has to be finite and resumable rather than merely fast.
INDEX_BUDGET_S = int(os.environ.get("AGENT_INDEX_BUDGET_S", "50"))

# Directories that are never knowledge, and are usually most of the files.
# Without this the walk descends into .git and node_modules and spends its
# time on objects nobody will ever search for.
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "out", "bin", "obj", "target", ".next", ".nuxt",
    ".gradle", ".idea", ".vscode", ".terraform", "vendor", "packages",
    "site-packages", "coverage", "htmlcov", ".cache", ".parcel-cache",
    "Debug", "Release", ".vs", "TestResults",
}

# Extensions worth naming so the report can say WHAT was skipped rather than
# just how much. Everything not in SUPPORTED_EXTS is skipped either way.
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg",
    ".tif", ".tiff", ".psd", ".ai", ".eps",
    ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".flac", ".ogg",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".iso", ".dmg",
    ".exe", ".dll", ".so", ".dylib", ".pdb", ".bin", ".dat",
    ".pq", ".pqout", ".parquet", ".diagnostics", ".map", ".lock",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".o", ".a", ".obj",
}
SUPPORTED_EXTS = TEXT_EXTS | DOC_EXTS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    mtime      REAL NOT NULL,
    chunks     INTEGER NOT NULL DEFAULT 0,
    embedded   INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    embedding  TEXT
);
CREATE TABLE IF NOT EXISTS watched_folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    added_at    TEXT NOT NULL,
    last_scan   TEXT,
    last_result TEXT
);
"""

_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
    text, content='doc_chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS doc_chunks_ai AFTER INSERT ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS doc_chunks_ad AFTER DELETE ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


class RagDependencyMissing(Exception):
    """A format needs an optional library that isn't installed."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------- #
# Local embeddings via Ollama (no data leaves the machine)
# ---------------------------------------------------------------------- #
def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed texts with the local Ollama embedding model. Returns a list of
    vectors, or None if Ollama/the model is unavailable (caller falls back to
    keyword search). Tries the batch endpoint, then the per-item endpoint."""
    if not texts:
        return []
    host = config.OLLAMA_HOST.rstrip("/")
    model = config.EMBED_MODEL

    # Newer batch endpoint: /api/embed -> {"embeddings": [[...], ...]}
    try:
        req = urllib.request.Request(
            f"{host}/api/embed",
            data=json.dumps({"model": model, "input": texts}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        embs = data.get("embeddings")
        if embs and len(embs) == len(texts):
            return [[float(x) for x in e] for e in embs]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        pass

    # Older per-item endpoint: /api/embeddings -> {"embedding": [...]}
    out: list[list[float]] = []
    for t in texts:
        try:
            req = urllib.request.Request(
                f"{host}/api/embeddings",
                data=json.dumps({"model": model, "prompt": t}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            emb = data.get("embedding")
            if not emb:
                return None
            out.append([float(x) for x in emb])
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            return None
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------- #
# Text extraction + chunking
# ---------------------------------------------------------------------- #
def read_file_text(path: Path) -> str | None:
    """Extract plain text from a file, or None if the type is unsupported."""
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        try:
            import pypdf
        except ImportError:
            raise RagDependencyMissing(
                "Reading PDFs needs pypdf. Install it:  pip install pypdf")
        try:
            reader = pypdf.PdfReader(str(path))
            return "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception:
            return ""
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise RagDependencyMissing(
                "Reading .docx needs python-docx. Install it:  pip install python-docx")
        try:
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception:
            return ""
    return None


def chunk_text(text: str, size: int | None = None,
               overlap: int | None = None) -> list[str]:
    """Split text into chunks on paragraph boundaries, windowing any
    paragraph that's longer than `size`."""
    size = size or config.RAG_CHUNK_CHARS
    overlap = overlap if overlap is not None else config.RAG_CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > size:                         # very long paragraph: window it
            if cur:
                chunks.append(cur)
                cur = ""
            step = max(1, size - overlap)
            for i in range(0, len(p), step):
                chunks.append(p[i:i + size])
        elif len(cur) + len(p) + 2 <= size:
            cur = (cur + "\n\n" + p) if cur else p
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------------- #
# Document store
# ---------------------------------------------------------------------- #
class DocumentStore:
    def __init__(self, db_path: Path | None = None, check_same_thread: bool = True):
        self.db_path = Path(db_path or config.RAG_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The single connection is shared across the UI worker threads, the
        # chat-time search, and the background folder watcher. SQLite objects
        # aren't safe for truly simultaneous use on one connection, so all
        # connection access is serialized through this re-entrant lock.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path),
                                    check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(_SCHEMA)
        try:
            self.conn.executescript(_FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.conn.commit()

    def ensure_schema(self) -> None:
        """Re-apply the (idempotent) schema. Called after restoring a backup."""
        self.conn.executescript(_SCHEMA)
        try:
            self.conn.executescript(_FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.conn.commit()

    # ---- ingestion ---------------------------------------------------- #
    def _flush(self, pending: list) -> tuple:
        """Embed a batch of files in one call, write them, commit.

        Committing here rather than at the very end is the difference
        between a three-hour run you can interrupt and one that loses
        everything if you do.
        """
        if not pending:
            self._last_embedded = False
            return 0, 0, 0
        flat, spans = [], []
        for item in pending:
            spans.append((len(flat), len(item["chunks"])))
            flat.extend(item["chunks"])
        vectors = embed_texts(flat)
        embedded = vectors is not None
        self._last_embedded = embedded

        added = updated = chunk_total = 0
        for item, (start, count) in zip(pending, spans):
            f = item["path"]
            if item["row"]:
                self.conn.execute("DELETE FROM documents WHERE id = ?",
                                  (item["row"]["id"],))
                updated += 1
            else:
                added += 1
            cur = self.conn.execute(
                "INSERT INTO documents (path, name, mtime, chunks, embedded, "
                "added_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(f), f.name, item["mtime"], count, int(embedded), _now()))
            doc_id = cur.lastrowid
            for i in range(count):
                emb = json.dumps(vectors[start + i]) if embedded else None
                self.conn.execute(
                    "INSERT INTO doc_chunks (doc_id, seq, text, embedding) "
                    "VALUES (?, ?, ?, ?)",
                    (doc_id, i, item["chunks"][i], emb))
            chunk_total += count
        self.conn.commit()          # progress is durable from here
        return added, updated, chunk_total

    def _collect_files(self, root: Path) -> list[Path]:
        """Files worth indexing, and — separately — how many were left out.

        The cap used to `break` silently, so indexing a folder of 2,000
        documents quietly gave you the alphabetically-first few hundred and
        said nothing. You'd search, get a confident answer drawn from a third
        of your material, and have no way to know the rest was never read.
        """
        self.last_skipped = {"over_limit": 0, "too_big": 0, "unsupported": 0}
        if root.is_file():
            return [root] if root.suffix.lower() in SUPPORTED_EXTS else []

        # Which files are already in the index? The cap used to be applied
        # BEFORE this was known, so a re-run collected the same
        # alphabetically-first N, found them unchanged, skipped them all, and
        # never looked at the rest. Re-indexing could not make progress —
        # which is exactly what it looked like from the outside.
        try:
            known = {r[0] for r in self.conn.execute(
                "SELECT path FROM documents").fetchall()}
        except Exception:
            known = set()

        eligible, binaries = [], 0
        # os.scandir with pruning, not rglob. rglob descends into every
        # directory before anything can be filtered, so a knowledge base with
        # a .git folder or node_modules spends most of its time walking files
        # that could never be indexed — which is why a 36,000-file folder
        # looked like it had hung when it was merely grinding.
        import os as _os
        stack = [str(root)]
        while stack:
            here = stack.pop()
            try:
                with _os.scandir(here) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in SKIP_DIRS or \
                                        entry.name.startswith("."):
                                    continue
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            ext = Path(entry.name).suffix.lower()
                            if ext not in SUPPORTED_EXTS:
                                # named separately so the report can say what
                                # it skipped, not merely that it skipped
                                if ext in BINARY_EXTS:
                                    binaries += 1
                                else:
                                    self.last_skipped["unsupported"] += 1
                                continue
                            if entry.stat().st_size > \
                                    config.RAG_MAX_FILE_MB * 1024 * 1024:
                                self.last_skipped["too_big"] += 1
                                continue
                            eligible.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue
        self.last_skipped["binary"] = binaries
        eligible.sort()

        # not-yet-indexed first, so running it again picks up where it
        # stopped rather than re-treading the same ground
        fresh = [f for f in eligible if str(f) not in known]
        seen_before = [f for f in eligible if str(f) in known]
        ordered = fresh + seen_before
        cap = max(1, config.RAG_MAX_FILES)
        # what's LEFT is the unread files this run won't reach — counting all
        # eligible files instead reported the same number every run, so it
        # looked like no progress was being made when it was
        self.last_skipped["over_limit"] = max(0, len(fresh) - cap)
        return ordered[:cap]

    def _collect_files_unused(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for f in sorted(root.rglob("*")):
            if len(files) >= config.RAG_MAX_FILES:
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
                    self.last_skipped["over_limit"] += 1
                continue
            if not f.is_file():
                continue
            if f.suffix.lower() not in SUPPORTED_EXTS:
                self.last_skipped["unsupported"] += 1
                continue
            try:
                if f.stat().st_size > config.RAG_MAX_FILE_MB * 1024 * 1024:
                    self.last_skipped["too_big"] += 1
                    continue
            except OSError:
                continue
            files.append(f)
        return files

    def ingest_path(self, path_str: str, budget_s: int = None,
                    on_progress=None) -> dict:
        """Index a file or (recursively) a folder. Returns a summary dict."""
        with self._lock:
            return self._ingest_path(path_str, budget_s,
                                     on_progress)

    def _ingest_path(self, path_str: str, budget_s: int = None,
                     on_progress=None) -> dict:
        root = Path(path_str).expanduser()
        if not root.exists():
            return {"error": f"Path not found: {root}"}
        files = self._collect_files(root)
        if not files:
            return {"error": "No supported files found there. Supported: text/code "
                             "files, plus .pdf and .docx (with the optional libraries)."}

        added = updated = skipped = chunk_total = 0
        pending, pending_chunks = [], 0
        stopped_early = False
        budget_s = INDEX_BUDGET_S if budget_s is None else budget_s
        t_start = _time.time()
        self._last_embedded = False
        missing_dep = None
        any_embedded = False
        for f in files:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            row = self.conn.execute(
                "SELECT id, mtime FROM documents WHERE path = ?", (str(f),)
            ).fetchone()
            if row and abs(row["mtime"] - mtime) < 1e-6:
                skipped += 1
                continue
            try:
                text = read_file_text(f)
            except RagDependencyMissing as exc:
                missing_dep = str(exc)
                skipped += 1
                continue
            except Exception:
                skipped += 1
                continue
            if not text or not text.strip():
                skipped += 1
                continue
            chunks = chunk_text(text)
            if not chunks:
                skipped += 1
                continue

            # Queue rather than embed now. Embedding one file at a time
            # meant one HTTP round-trip PER FILE — on 12,595 files that is
            # over an hour of pure latency before any work happens. Batching
            # across files turns thousands of round-trips into dozens.
            pending.append({"path": f, "mtime": mtime, "row": row,
                            "chunks": chunks})
            pending_chunks += len(chunks)

            if pending_chunks >= EMBED_BATCH:
                a, u, c = self._flush(pending)
                added += a
                updated += u
                chunk_total += c
                any_embedded = any_embedded or self._last_embedded
                pending, pending_chunks = [], 0
                done_files = added + updated
                if on_progress:
                    on_progress({"done": done_files, "of": len(files),
                                 "chunks": chunk_total})
                # Stop cleanly on a budget instead of running for hours.
                # Everything committed so far stays; the next run continues
                # from where this one stopped, because unindexed files are
                # collected first.
                if budget_s and (_time.time() - t_start) > budget_s:
                    stopped_early = True
                    break

        if pending:
            a, u, c = self._flush(pending)
            added += a
            updated += u
            chunk_total += c
            any_embedded = any_embedded or self._last_embedded
        self.conn.commit()

        result = {"added": added, "updated": updated, "skipped": skipped,
                  "chunks": chunk_total,
                  "seconds": round(_time.time() - t_start, 1),
                  "mode": "semantic" if any_embedded else "keyword"}
        if stopped_early:
            remaining = max(0, len(files) - (added + updated))
            result["stopped_early"] = True
            result["remaining_here"] = remaining
            result["run_again"] = True
            result["warning"] = (
                f"Indexed {added + updated} file(s) in this run and stopped "
                f"at the time limit with {remaining} still to go in this "
                f"batch. **Everything done so far is saved** — run it again "
                f"and it continues. It works this way because a single run "
                f"over thousands of files outlives any browser request, and "
                f"a run that loses its work when interrupted is worse than "
                f"a slow one.")
        # If the cap bit, say so. A search over a third of someone's material
        # that doesn't mention the other two thirds is worse than one that
        # refuses — the answer looks complete either way.
        left = getattr(self, "last_skipped", {}) or {}
        if left.get("over_limit"):
            result["truncated"] = left["over_limit"]
            result["warning"] = (
                f"{config.RAG_MAX_FILES} files indexed this run — "
                f"{left['over_limit']} still to go. **Run it again** and it "
                f"continues with the next batch; it takes the ones it hasn't "
                f"read yet, so each run makes progress. Or raise "
                f"RAG_MAX_FILES in Settings to do the lot at once. Until it's "
                f"finished, searches answer from the part that is indexed "
                f"without saying so.")
            result["run_again"] = True
        if left.get("binary"):
            result["skipped_binary"] = left["binary"]
            result.setdefault("note", "")
            result["note"] = ((result.get("note", "") + " ") if
                              result.get("note") else "") + (
                f"{left['binary']:,} image/binary file(s) were skipped "
                f"without being opened — they aren't knowledge and reading "
                f"them is what makes a big folder look like it has hung.")
        if left.get("too_big"):
            result["too_big"] = left["too_big"]
            result.setdefault("warning", "")
            result["warning"] += (
                (" " if result.get("warning") else "")
                + f"{left['too_big']} file(s) were over the "
                  f"{config.RAG_MAX_FILE_MB} MB limit and skipped.")
        if missing_dep:
            result["note"] = missing_dep
        if not any_embedded and (added or updated):
            result["note"] = (
                f"Indexed by keyword only (couldn't reach the embedding model "
                f"'{config.EMBED_MODEL}'). For smarter semantic search run: "
                f"ollama pull {config.EMBED_MODEL}")
        return result

    # ---- search ------------------------------------------------------- #
    def search(self, query: str, k: int | None = None) -> list[dict]:
        """Return the most relevant chunks. Semantic if embeddings exist,
        else FTS5 keyword search."""
        with self._lock:
            return self._search(query, k)

    def _search(self, query: str, k: int | None = None) -> list[dict]:
        k = k or config.RAG_TOP_K
        query = (query or "").strip()
        if not query:
            return []

        embedded_rows = self.conn.execute(
            "SELECT c.id, c.text, c.embedding, d.name FROM doc_chunks c "
            "JOIN documents d ON d.id = c.doc_id WHERE c.embedding IS NOT NULL"
        ).fetchall()

        if embedded_rows:
            qv = embed_texts([query])
            if qv:
                q = qv[0]
                scored = []
                for r in embedded_rows:
                    try:
                        vec = json.loads(r["embedding"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    scored.append((_cosine(q, vec), r["text"], r["name"]))
                scored.sort(key=lambda x: x[0], reverse=True)
                return [{"text": t, "source": n, "score": round(s, 3)}
                        for s, t, n in scored[:k] if s > 0.0]

        return self._keyword_search(query, k)

    def _keyword_search(self, query: str, k: int) -> list[dict]:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2]
        if not words:
            return []
        if self.fts:
            fts_q = " OR ".join(dict.fromkeys(words[:16]))
            try:
                rows = self.conn.execute(
                    "SELECT c.text, d.name, bm25(doc_chunks_fts) AS rank "
                    "FROM doc_chunks_fts JOIN doc_chunks c ON c.id = doc_chunks_fts.rowid "
                    "JOIN documents d ON d.id = c.doc_id "
                    "WHERE doc_chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_q, k)).fetchall()
                return [{"text": r["text"], "source": r["name"], "score": None}
                        for r in rows]
            except sqlite3.OperationalError:
                pass
        # last-resort LIKE
        scored: dict[int, int] = {}
        for w in set(words[:16]):
            for r in self.conn.execute(
                    "SELECT id FROM doc_chunks WHERE text LIKE ? LIMIT 50", (f"%{w}%",)):
                scored[r["id"]] = scored.get(r["id"], 0) + 1
        if not scored:
            return []
        top = sorted(scored, key=scored.get, reverse=True)[:k]
        ph = ",".join("?" * len(top))
        rows = self.conn.execute(
            f"SELECT c.text, d.name FROM doc_chunks c JOIN documents d ON d.id = c.doc_id "
            f"WHERE c.id IN ({ph})", top).fetchall()
        return [{"text": r["text"], "source": r["name"], "score": None} for r in rows]

    # ---- management --------------------------------------------------- #
    def list_documents(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, path, chunks, embedded, added_at FROM documents "
                "ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def remove_document(self, doc_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def clear(self) -> int:
        with self._lock:
            n = self.doc_count()
            self.conn.execute("DELETE FROM documents")
            self.conn.commit()
            return n

    def doc_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def chunk_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]

    # ---- auto-watched folders ---------------------------------------- #
    def add_watched_folder(self, path_str: str) -> dict:
        """Register a folder for automatic re-indexing. Returns a status dict."""
        p = Path(path_str or "").expanduser()
        if not str(p).strip():
            return {"error": "Enter a folder path."}
        if not p.exists() or not p.is_dir():
            return {"error": f"Not a folder: {p}"}
        norm = str(p)
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM watched_folders WHERE path = ?", (norm,)).fetchone()
            if existing:
                return {"id": existing["id"], "path": norm, "already": True}
            cur = self.conn.execute(
                "INSERT INTO watched_folders (path, added_at) VALUES (?, ?)",
                (norm, _now()))
            self.conn.commit()
            return {"id": cur.lastrowid, "path": norm, "already": False}

    def list_watched_folders(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, path, added_at, last_scan, last_result FROM "
                "watched_folders ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def remove_watched_folder(self, folder_id: int) -> bool:
        """Stop watching a folder. Already-indexed documents are kept."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM watched_folders WHERE id = ?", (folder_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def _prune_missing(self, folder_str: str) -> int:
        """Remove indexed documents under `folder` whose files no longer exist."""
        root = str(Path(folder_str).expanduser())
        prefix = root + os.sep
        removed = 0
        rows = self.conn.execute("SELECT id, path FROM documents").fetchall()
        for r in rows:
            pth = r["path"]
            if pth == root or pth.startswith(prefix):
                if not os.path.exists(pth):
                    self.conn.execute("DELETE FROM documents WHERE id = ?", (r["id"],))
                    removed += 1
        if removed:
            self.conn.commit()
        return removed

    def rescan_all_watched(self) -> dict:
        """Re-index every watched folder (cheap: unchanged files are skipped),
        prune files deleted from disk, and record per-folder results."""
        with self._lock:
            folders = self.list_watched_folders()
            total_added = total_updated = total_pruned = 0
            for f in folders:
                res = self._ingest_path(f["path"])
                if "error" in res:
                    summary = res["error"]
                else:
                    pruned = self._prune_missing(f["path"])
                    total_added += res.get("added", 0)
                    total_updated += res.get("updated", 0)
                    total_pruned += pruned
                    summary = (f"{res.get('added', 0)} new, "
                               f"{res.get('updated', 0)} updated, "
                               f"{pruned} removed")
                self.conn.execute(
                    "UPDATE watched_folders SET last_scan = ?, last_result = ? "
                    "WHERE id = ?", (_now(), summary[:200], f["id"]))
            self.conn.commit()
            return {"folders": len(folders), "added": total_added,
                    "updated": total_updated, "pruned": total_pruned}


# Module-level singleton so tools/UI share one store without threading it
# through every signature.
_store: DocumentStore | None = None


def get_store() -> DocumentStore:
    global _store
    if _store is None:
        _store = DocumentStore(check_same_thread=False)
    return _store


def survey(path: str, sample: int = 40) -> dict:
    """What's in a folder, and what indexing it would actually involve.

    Written after a 36,500-file / 4.3 GB knowledge base appeared to hang: it
    wasn't stuck, it was grinding through 23,000 screenshots. Nothing told the
    user that, because nothing had looked before starting. Counting first
    costs seconds and turns "it never finishes" into a number.
    """
    import os
    from pathlib import Path as _P
    root = _P(path).expanduser()
    if not root.exists():
        return {"ok": False, "error": f"{root} isn't there"}
    if root.is_file():
        return {"ok": True, "files": 1, "indexable": 1,
                "estimate": "one file — seconds"}

    kinds, skipped_dirs = {}, 0
    indexable, indexable_bytes = 0, 0
    binary, binary_bytes = 0, 0
    other, too_big = 0, 0
    sample_paths = []
    stack = [str(root)]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in SKIP_DIRS or \
                                    entry.name.startswith("."):
                                skipped_dirs += 1
                                continue
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        ext = _P(entry.name).suffix.lower() or "(none)"
                        size = entry.stat().st_size
                        kinds[ext] = kinds.get(ext, 0) + 1
                        if ext in SUPPORTED_EXTS:
                            if size > config.RAG_MAX_FILE_MB * 1024 * 1024:
                                too_big += 1
                            else:
                                indexable += 1
                                indexable_bytes += size
                                if len(sample_paths) < sample:
                                    sample_paths.append(entry.name)
                        elif ext in BINARY_EXTS:
                            binary += 1
                            binary_bytes += size
                        else:
                            other += 1
                    except OSError:
                        continue
        except OSError:
            continue

    total = indexable + binary + other + too_big
    # ~700 characters a chunk, and embedding runs at roughly 40 chunks a
    # second locally — a rough figure, and labelled as one
    chunks = max(1, int(indexable_bytes / 700))
    seconds = int(chunks / 40)
    big_kinds = sorted(kinds.items(), key=lambda kv: -kv[1])[:8]
    return {
        "ok": True, "root": str(root),
        "files": total, "indexable": indexable,
        "indexable_mb": round(indexable_bytes / 1e6, 1),
        "skipped_binary": binary, "binary_gb": round(binary_bytes / 1e9, 2),
        "skipped_other": other, "too_big": too_big,
        "skipped_folders": skipped_dirs,
        "by_extension": [{"ext": k, "files": v} for k, v in big_kinds],
        "estimated_chunks": chunks,
        "estimate": _estimate_words(seconds),
        "sample": sample_paths[:12],
        "verdict": (
            f"{total:,} files, of which **{indexable:,} can be indexed** "
            f"({round(indexable_bytes / 1e6, 1)} MB). "
            + (f"{binary:,} images and binaries ({round(binary_bytes / 1e9, 2)} "
               f"GB) are skipped without being opened. " if binary else "")
            + (f"{skipped_dirs} build/vendor folder(s) skipped entirely. "
               if skipped_dirs else "")
            + f"Roughly {chunks:,} chunks, {_estimate_words(seconds)}."),
        "note": ("An estimate from file sizes, not a measurement. Indexing "
                 "reports its progress as it goes."),
    }


def _estimate_words(seconds: int) -> str:
    if seconds < 90:
        return "under a couple of minutes"
    if seconds < 3600:
        return f"about {max(1, seconds // 60)} minutes"
    return f"about {seconds / 3600:.1f} hours — worth narrowing the folder"
