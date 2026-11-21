"""SQLite 存储层（仅标准库）。

每份转录稿（document）独立维护：原文、实体标注、勘误流水、片段表、
标注受影响关系。勘误号在文档内从 1 开始递增，撤回后号码保留、状态置为
retracted，不复用。
"""

from __future__ import annotations

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    original_text TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    number      INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('person', 'place')),
    name        TEXT NOT NULL,
    start       INTEGER NOT NULL,
    "end"       INTEGER NOT NULL,
    UNIQUE (document_id, number)
);
CREATE TABLE IF NOT EXISTS corrections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    number         INTEGER NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('insert', 'delete', 'replace')),
    text           TEXT NOT NULL,            -- 本笔写入的文本（纯删除为空串）
    range_start    INTEGER NOT NULL,         -- 应用时在当期版本上的区间（审计用）
    range_end      INTEGER NOT NULL,
    deleted_text   TEXT NOT NULL DEFAULT '', -- 本笔删掉的文本（审计用）
    deleted_pieces TEXT NOT NULL,            -- JSON，撤回时据此还原
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, number)
);
CREATE TABLE IF NOT EXISTS correction_conflicts (
    document_id       INTEGER NOT NULL,
    correction_number INTEGER NOT NULL,  -- 后发生的勘误
    target_number     INTEGER NOT NULL,  -- 被它触及的更早勘误
    PRIMARY KEY (document_id, correction_number, target_number)
);
CREATE TABLE IF NOT EXISTS pieces (
    document_id INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('orig', 'ins', 'seam')),
    src         INTEGER,                   -- ins/seam 对应的勘误号
    a           INTEGER NOT NULL,
    b           INTEGER NOT NULL,
    PRIMARY KEY (document_id, seq)
);
CREATE TABLE IF NOT EXISTS entity_effects (
    document_id       INTEGER NOT NULL,
    entity_id         INTEGER NOT NULL,
    correction_number INTEGER NOT NULL,  -- 影响该标注的仍生效勘误
    PRIMARY KEY (document_id, entity_id, correction_number)
);
CREATE TABLE IF NOT EXISTS word_terms (
    term TEXT PRIMARY KEY,               -- 词表中的词组（逐字匹配）
    seq  INTEGER NOT NULL                -- 词表内顺序，查询按此输出
);
CREATE TABLE IF NOT EXISTS word_stats (
    document_id    INTEGER NOT NULL REFERENCES documents(id),
    term           TEXT NOT NULL,
    original_count INTEGER NOT NULL DEFAULT 0,  -- 原稿出现次数
    current_count  INTEGER NOT NULL DEFAULT 0,  -- 现稿出现次数
    PRIMARY KEY (document_id, term)
);
CREATE INDEX IF NOT EXISTS idx_word_stats_term ON word_stats(term);
CREATE TABLE IF NOT EXISTS wordstats_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "app.db")


def init_db() -> None:
    path = db_path()
    if path != ":memory:":
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if db_path() != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---- 片段表 ----

def load_pieces(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, src, a, b FROM pieces WHERE document_id = ? ORDER BY seq",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_pieces(conn: sqlite3.Connection, doc_id: int, pieces: list[dict]) -> None:
    conn.execute("DELETE FROM pieces WHERE document_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO pieces (document_id, seq, kind, src, a, b) VALUES (?, ?, ?, ?, ?, ?)",
        [(doc_id, i, p["kind"], p["src"], p["a"], p["b"]) for i, p in enumerate(pieces)],
    )


# ---- 勘误 ----

def correction_texts(conn: sqlite3.Connection, doc_id: int) -> dict[int, str]:
    rows = conn.execute(
        "SELECT number, text FROM corrections WHERE document_id = ?", (doc_id,)
    ).fetchall()
    return {r["number"]: r["text"] for r in rows}


def next_correction_number(conn: sqlite3.Connection, doc_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM corrections WHERE document_id = ?",
        (doc_id,),
    ).fetchone()
    return row["n"]


def active_conflicts_against(conn: sqlite3.Connection, doc_id: int, number: int) -> list[int]:
    """仍生效、且与编号 number 的勘误冲突的后续勘误号。"""
    rows = conn.execute(
        """
        SELECT cc.correction_number AS n
        FROM correction_conflicts cc
        JOIN corrections c
          ON c.document_id = cc.document_id AND c.number = cc.correction_number
        WHERE cc.document_id = ? AND cc.target_number = ? AND c.status = 'active'
        ORDER BY cc.correction_number
        """,
        (doc_id, number),
    ).fetchall()
    return [r["n"] for r in rows]


# ---- 专题词频 ----

def word_terms(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT term FROM word_terms ORDER BY seq, term").fetchall()
    return [r["term"] for r in rows]


def replace_word_terms(conn: sqlite3.Connection, terms: list[str]) -> None:
    conn.execute("DELETE FROM word_terms")
    conn.executemany(
        "INSERT OR IGNORE INTO word_terms (term, seq) VALUES (?, ?)",
        [(t, i) for i, t in enumerate(terms)],
    )


def word_stat_rows(conn: sqlite3.Connection, doc_id: int) -> dict[str, tuple[int, int]]:
    rows = conn.execute(
        "SELECT term, original_count, current_count FROM word_stats"
        " WHERE document_id = ?",
        (doc_id,),
    ).fetchall()
    return {r["term"]: (r["original_count"], r["current_count"]) for r in rows}


def save_word_stats(conn: sqlite3.Connection, doc_id: int,
                    stats: dict[str, tuple[int, int]]) -> None:
    """整体覆盖某文档的统计；没有任何出现的词不留行。"""
    conn.execute("DELETE FROM word_stats WHERE document_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO word_stats (document_id, term, original_count, current_count)"
        " VALUES (?, ?, ?, ?)",
        [(doc_id, t, oc, cc) for t, (oc, cc) in stats.items()
         if oc or cc],
    )


def delete_word_stats(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM word_stats WHERE document_id = ?", (doc_id,))


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM wordstats_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO wordstats_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---- JSON 序列化 ----

def dumps_pieces(pieces: list[dict]) -> str:
    return json.dumps(pieces, ensure_ascii=False)


def loads_pieces(payload: str) -> list[dict]:
    return json.loads(payload)
