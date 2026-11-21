"""业务操作层（框架无关）：导入、勘误、撤回、版本查询、实体检索、专题词频。

所有写操作在单个事务内完成；版本查询在一次读事务内取正文与标注，
保证“从同一版返回”。专题词频的查询只读已保存的统计，不重新扫描正文。
"""

from __future__ import annotations

import json
import os
import sqlite3

from . import db, engine

WORDLIST_ENV_KEYS = (
    "WORDLIST_PATH", "WORD_LIST_PATH", "WORDLIST_FILE", "WORDLIST",
    "WORD_LIST", "WORDS_PATH", "TERMS_PATH", "TOPIC_WORDS_PATH",
)
WORDLIST_INIT_KEY = "wordlist_initialized"
# 未显式配置时依次尝试的本地词表位置（存在才启用）
DEFAULT_WORDLIST_PATHS = (
    "/config/wordlist.txt", "/app/wordlist.txt", "/data/wordlist.txt",
    "wordlist.txt", "words.txt", "terms.txt", "wordlist.json", "words.json",
)



class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    def __init__(self, message: str, conflicts: list[int]):
        super().__init__(message)
        self.conflicts = conflicts


# ---------------------------------------------------------------- 导入

def import_document(conn: sqlite3.Connection, text: str, entities: list[dict]) -> dict:
    """导入正文及带编号、按字符区间标出的人名/地名。"""
    used: set[int] = set()
    for ent in entities:
        n = ent.get("number")
        if n is not None:
            if n in used:
                raise ValidationError(f"实体编号重复: {n}")
            used.add(n)

    assigned: list[dict] = []
    next_n = 1
    for ent in entities:
        ent = dict(ent)
        if ent.get("number") is None:
            while next_n in used:
                next_n += 1
            ent["number"] = next_n
            used.add(next_n)
        s, t = ent["start"], ent["end"]
        if not (0 <= s < t <= len(text)):
            raise ValidationError(
                f"实体 {ent['number']} 区间 [{s}, {t}) 超出正文范围或为空")
        assigned.append(ent)

    cur = conn.execute(
        "INSERT INTO documents (original_text) VALUES (?)", (text,))
    doc_id = cur.lastrowid
    conn.executemany(
        'INSERT INTO entities (document_id, number, kind, name, start, "end")'
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(doc_id, e["number"], e["kind"], e["name"], e["start"], e["end"])
         for e in assigned],
    )
    db.save_pieces(conn, doc_id, [engine.piece("orig", None, 0, len(text))])
    # 新稿：原稿与现稿内容相同，两套统计都计入
    terms = db.word_terms(conn)
    if terms:
        stats = {t: (engine.count_occurrences(text, t),
                     engine.count_occurrences(text, t))
                 for t in terms}
        db.save_word_stats(conn, doc_id, stats)
    return {"document_id": doc_id, "revision": 0, "entity_count": len(assigned)}


# ---------------------------------------------------------------- 勘误

def apply_correction(conn: sqlite3.Connection, doc_id: int,
                     s: int, e: int, text: str) -> dict:
    """按当前版把 [s, e) 替换为 text，返回勘误号。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    total = engine.total_length(pieces)
    if not (0 <= s <= e <= total):
        raise ValidationError(f"区间 [{s}, {e}) 超出当前版长度 {total}")
    if s == e and not text:
        raise ValidationError("空操作：区间为空且未提供替换文本")

    number = db.next_correction_number(conn, doc_id)
    kind = "insert" if s == e else ("delete" if not text else "replace")

    new_pieces, deleted, conflicts = engine.apply_edit(pieces, s, e, text, number)
    corr_texts = db.correction_texts(conn, doc_id)
    deleted_text = engine.render(deleted, doc["original_text"], corr_texts)

    # 标注受影响判定：用应用前的片段表（与 [s, e) 同一坐标系）
    effects = []
    for ent in _entities(conn, doc_id):
        ca, cb = engine.current_range(pieces, ent["start"], ent["end"])
        if engine.edit_touches_span(s, e, bool(text), ca, cb):
            effects.append(ent["id"])

    conn.execute(
        "INSERT INTO corrections"
        " (document_id, number, kind, text, range_start, range_end,"
        "  deleted_text, deleted_pieces)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, number, kind, text, s, e, deleted_text, db.dumps_pieces(deleted)),
    )
    conn.executemany(
        "INSERT INTO correction_conflicts (document_id, correction_number, target_number)"
        " VALUES (?, ?, ?)",
        [(doc_id, number, t) for t in sorted(conflicts)],
    )
    conn.executemany(
        "INSERT INTO entity_effects (document_id, entity_id, correction_number)"
        " VALUES (?, ?, ?)",
        [(doc_id, ent_id, number) for ent_id in effects],
    )
    db.save_pieces(conn, doc_id, new_pieces)
    _recount_current_after_edit(
        conn, doc_id, doc["original_text"], pieces, new_pieces, corr_texts,
        (s, e), len(text))
    revision = _bump_revision(conn, doc_id)
    return {"correction_number": number, "kind": kind, "revision": revision}


def retract_correction(conn: sqlite3.Connection, doc_id: int, number: int) -> dict:
    """按号撤回一笔勘误。

    若仍生效的后续勘误改删了该笔写入的文字、在其中插字，或在该笔纯删除
    留下的接缝插字/跨缝改删，则抛出 ConflictError（携带冲突勘误号），
    当前版保持不变；否则只还原这一笔。
    """
    _get_document(conn, doc_id)
    row = conn.execute(
        "SELECT * FROM corrections WHERE document_id = ? AND number = ?",
        (doc_id, number),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"勘误号不存在: {number}")
    if row["status"] == "retracted":
        raise ValidationError(f"勘误 {number} 已撤回")

    conflicts = db.active_conflicts_against(conn, doc_id, number)
    if conflicts:
        raise ConflictError("存在仍生效的冲突后续勘误，保留现版", conflicts)

    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    deleted = db.loads_pieces(row["deleted_pieces"])
    new_pieces = engine.revert_edit(pieces, deleted, number, row["kind"])

    db.save_pieces(conn, doc_id, new_pieces)
    conn.execute(
        "UPDATE corrections SET status = 'retracted'"
        " WHERE document_id = ? AND number = ?",
        (doc_id, number),
    )
    # 该笔不再生效：它对标注的影响随之解除
    conn.execute(
        "DELETE FROM entity_effects WHERE document_id = ? AND correction_number = ?",
        (doc_id, number),
    )
    # 撤回后按实际还原的位置更新统计：前文若因其他勘误移动过落点，
    # 以还原后的片段表为准重新定位，而不是套用当初应用时的坐标。
    if row["kind"] == "delete":
        # 纯删除：本笔在撤回前的接缝位置就是还原文字的落点
        pos = engine.locate_seam(pieces, number)
        start = end = 0 if pos is None else pos
        restored_length = len(row["deleted_text"])
    else:
        loc = engine.locate_inserted(pieces, number)
        start, end = loc if loc is not None else (0, 0)
        restored_length = sum(p["b"] - p["a"] for p in deleted)
    _recount_current_after_edit(
        conn, doc_id, doc["original_text"], pieces, new_pieces,
        db.correction_texts(conn, doc_id),
        (start, end), restored_length)
    revision = _bump_revision(conn, doc_id)
    return {"retracted": number, "revision": revision}


# ---------------------------------------------------------------- 查询

def get_version(conn: sqlite3.Connection, doc_id: int) -> dict:
    """从同一版返回正文、有效标注的当前字符区间及待复核标注。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    text = engine.render(pieces, doc["original_text"],
                         db.correction_texts(conn, doc_id))
    valid, pending = [], []
    for ent in _entities(conn, doc_id):
        item = _entity_view(pieces, ent, _effects_map(conn, doc_id))
        (pending if item["affecting_corrections"] else valid).append(item)
    return {
        "document_id": doc_id,
        "revision": doc["revision"],
        "text": text,
        "valid_entities": valid,
        "pending_entities": pending,
    }


def search_entities(conn: sqlite3.Connection, doc_id: int,
                    kind: str | None = None, q: str | None = None) -> list[dict]:
    """实体检索：按类型（人名/地名）和名称子串过滤，返回当前区间与状态。"""
    _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    effects = _effects_map(conn, doc_id)
    out = []
    for ent in _entities(conn, doc_id):
        if kind and ent["kind"] != kind:
            continue
        if q and q not in ent["name"]:
            continue
        out.append(_entity_view(pieces, ent, effects))
    return out


def list_corrections(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    _get_document(conn, doc_id)
    rows = conn.execute(
        "SELECT * FROM corrections WHERE document_id = ? ORDER BY number", (doc_id,)
    ).fetchall()
    conflicts = conn.execute(
        "SELECT correction_number, target_number FROM correction_conflicts"
        " WHERE document_id = ? ORDER BY correction_number, target_number",
        (doc_id,),
    ).fetchall()
    cmap: dict[int, list[int]] = {}
    for c in conflicts:
        cmap.setdefault(c["correction_number"], []).append(c["target_number"])
    return [
        {
            "number": r["number"],
            "kind": r["kind"],
            "text": r["text"],
            "range_start": r["range_start"],
            "range_end": r["range_end"],
            "deleted_text": r["deleted_text"],
            "status": r["status"],
            "touches_corrections": cmap.get(r["number"], []),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id, d.revision, d.created_at,
               length(d.original_text) AS original_length,
               (SELECT COUNT(*) FROM entities e WHERE e.document_id = d.id) AS entity_count,
               (SELECT COUNT(*) FROM corrections c
                 WHERE c.document_id = d.id AND c.status = 'active') AS active_corrections
        FROM documents d ORDER BY d.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 专题词频

def configured_wordlist_path() -> str | None:
    """本地词表路径：优先环境变量，其次约定位置中第一个存在的文件。"""
    for key in WORDLIST_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return value
    for candidate in DEFAULT_WORDLIST_PATHS:
        if os.path.isfile(candidate):
            return candidate
    return None


def read_wordlist(path: str) -> list[str]:
    """读取本地词表。支持 JSON 数组/对象，或每行一个词组的文本文件。

    纯文本容忍逗号/分号/顿号分隔（每行可含多个词组），# 开头的行是注释。
    空白行忽略，重复词组保留第一次出现的顺序。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return parse_wordlist(raw, path=path)


def parse_wordlist(raw: str, path: str | None = None) -> list[str]:
    """解析词表内容（read_wordlist 的纯函数版本）。"""
    if ((path and path.lower().endswith(".json"))
            or raw.lstrip().startswith(("[", "{"))):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = (data.get("terms") or data.get("words")
                    or data.get("wordlist") or [])
        if not isinstance(data, list):
            raise ValidationError("词表 JSON 必须是数组或含 terms 的对象")
        terms = [str(t) for t in data]
    else:
        # 纯文本：按行读取，同时容忍逗号/分号/顿号分隔（每行可含多个词组）
        import re
        terms = []
        for line in raw.splitlines():
            if line.lstrip().startswith("#"):
                continue
            terms.extend(re.split(r"[,，、;；\t]", line))
    return _normalize_terms(terms)


def _normalize_terms(terms) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        t = str(t).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def terms_from_env() -> list[str]:
    """从环境变量读取行内词表（WORDLIST_TERMS，逗号/分号分隔）。"""
    for key in ("WORDLIST_TERMS", "WORD_TERMS", "TERMS", "TOPIC_WORDS"):
        raw = os.environ.get(key)
        if raw:
            import re
            return _normalize_terms(re.split(r"[,，;；\n]", raw))
    return []


def initialize_word_stats_from_env(conn: sqlite3.Connection) -> dict:
    """首次启动时从本地词表启用统计并纳入已有稿件。

    已经初始化过则沿用库中保存的词表，不再读文件、不重复累计。
    """
    if db.meta_get(conn, WORDLIST_INIT_KEY) == "1":
        return {"enabled": True, "reused": True, "terms": len(db.word_terms(conn))}
    terms = terms_from_env()
    if not terms:
        path = configured_wordlist_path()
        if path and os.path.isfile(path):
            try:
                terms = read_wordlist(path)
            except (OSError, ValueError, ValidationError):
                terms = []
    if not terms:
        return {"enabled": False, "reused": False, "terms": 0}
    initialize_word_stats(conn, terms=terms)
    return {"enabled": True, "reused": False, "terms": len(db.word_terms(conn))}


def initialize_word_stats(conn: sqlite3.Connection, terms: list[str] | None = None,
                          replace: bool = False, path: str | None = None) -> dict:
    """启用/更新专题词频统计，并为所有已有稿件补齐统计。

    - 首次启用：写入词表并纳入全部已有稿件（原稿与现稿都统计）。
    - 已启用：默认沿用库中词表，不再读本地文件；显式给出新词组时追加。
    - replace=True：整体替换词表并重算已有稿件。
    """
    initialized = db.meta_get(conn, WORDLIST_INIT_KEY) == "1"
    if terms is None:
        terms = terms_from_env()
        if not terms:
            configured = path or configured_wordlist_path()
            if configured and os.path.isfile(configured):
                terms = read_wordlist(configured)
    terms = _normalize_terms(terms or [])

    existing = db.word_terms(conn)
    doc_rows = conn.execute(
        "SELECT id, original_text FROM documents ORDER BY id").fetchall()

    if not initialized or replace:
        chosen = terms
        if chosen != existing:
            db.replace_word_terms(conn, chosen)
        _drop_stats_for_terms(conn, set(chosen))
        recompute = set(chosen)
        force = True
    elif terms:
        added = [t for t in terms if t not in set(existing)]
        if not added:
            return _init_summary(existing, doc_rows)
        chosen = existing + added
        db.replace_word_terms(conn, chosen)
        recompute = set(added)
        force = False
    else:
        # 重启后沿用已保存的词表；仅补齐缺失的统计
        if not existing:
            return {"enabled": False, "initialized": True,
                    "term_count": 0, "document_count": len(doc_rows)}
        for doc in doc_rows:
            _backfill_document_stats(conn, doc["id"], doc["original_text"],
                                     set(existing), force=False)
        db.meta_set(conn, WORDLIST_INIT_KEY, "1")
        return _init_summary(existing, doc_rows)

    for doc in doc_rows:
        _backfill_document_stats(conn, doc["id"], doc["original_text"],
                                 recompute, force=force)

    if chosen:
        db.meta_set(conn, WORDLIST_INIT_KEY, "1")
        return _init_summary(chosen, doc_rows)
    return {"enabled": False, "initialized": initialized,
            "term_count": 0, "document_count": len(doc_rows)}


def _init_summary(terms: list[str], doc_rows) -> dict:
    return {
        "enabled": bool(terms),
        "initialized": True,
        "term_count": len(terms),
        "document_count": len(doc_rows),
    }


def _drop_stats_for_terms(conn: sqlite3.Connection, keep: set[str]) -> None:
    """词表被替换时，删除已不在词表中的词组统计。"""
    rows = conn.execute("SELECT DISTINCT term FROM word_stats").fetchall()
    stale = [r["term"] for r in rows if r["term"] not in keep]
    if stale:
        conn.executemany(
            "DELETE FROM word_stats WHERE term = ?", [(t,) for t in stale])


def _backfill_document_stats(conn: sqlite3.Connection, doc_id: int, original: str,
                             terms: set[str], force: bool = False) -> None:
    """为一份既有稿件计算指定词组的原稿/现稿统计。

    force=False 时只补缺失项（重启后不应覆盖已保存的增量结果）。
    """
    stats = db.word_stat_rows(conn, doc_id)
    todo = [t for t in terms if force or t not in stats]
    if not todo:
        return
    pieces = db.load_pieces(conn, doc_id)
    corr_texts = db.correction_texts(conn, doc_id)
    current = None
    for term in todo:
        oc = engine.count_occurrences(original, term)
        if current is None:
            current = engine.render(pieces, original, corr_texts)
        cc = engine.count_occurrences(current, term)
        stats[term] = (oc, cc)
    db.save_word_stats(conn, doc_id, stats)


def list_word_stats(conn: sqlite3.Connection, terms: list[str] | None = None) -> list[dict]:
    """查询已保存的统计（不重新扫描正文）。

    返回词表中每个词组的原稿/现稿总出现次数与涉及稿数。
    """
    rows = conn.execute(
        """
        SELECT t.term AS term,
               COALESCE(SUM(s.original_count), 0) AS original_count,
               COALESCE(SUM(s.current_count), 0) AS current_count,
               COALESCE(SUM(CASE WHEN s.original_count > 0 THEN 1 ELSE 0 END), 0)
                   AS original_document_count,
               COALESCE(SUM(CASE WHEN s.current_count > 0 THEN 1 ELSE 0 END), 0)
                   AS current_document_count
        FROM word_terms t
        LEFT JOIN word_stats s ON s.term = t.term
        GROUP BY t.term, t.seq
        ORDER BY t.seq, t.term
        """
    ).fetchall()
    wanted = set(terms) if terms else None
    return [_word_stat_view(r) for r in rows
            if wanted is None or r["term"] in wanted]


def get_word_stat(conn: sqlite3.Connection, term: str) -> dict:
    rows = list_word_stats(conn, [term])
    for item in rows:
        if item["term"] == term:
            return item
    raise NotFoundError(f"词组不在词表中: {term}")


def word_terms(conn: sqlite3.Connection) -> list[str]:
    return db.word_terms(conn)


def document_word_stats(conn: sqlite3.Connection, doc_id: int) -> dict:
    _get_document(conn, doc_id)
    stats = db.word_stat_rows(conn, doc_id)
    return {
        "document_id": doc_id,
        "terms": [
            {
                "term": term,
                "original_count": stats.get(term, (0, 0))[0],
                "current_count": stats.get(term, (0, 0))[1],
            }
            for term in db.word_terms(conn)
        ],
    }


def _word_stat_view(row) -> dict:
    oc, cc = row["original_count"], row["current_count"]
    od, cd = row["original_document_count"], row["current_document_count"]
    return {
        "term": row["term"],
        # 平铺字段（便于直接取用）
        "original_count": oc,
        "current_count": cc,
        "original_occurrences": oc,
        "current_occurrences": cc,
        "original_document_count": od,
        "current_document_count": cd,
        "original_documents": od,
        "current_documents": cd,
        "before_count": oc,
        "after_count": cc,
        "before_document_count": od,
        "after_document_count": cd,
        "before_occurrences": oc,
        "after_occurrences": cc,
        "before_documents": od,
        "after_documents": cd,
        # 分组字段：原稿 / 现稿
        "original": {"count": oc, "occurrences": oc,
                     "document_count": od, "documents": od},
        "current": {"count": cc, "occurrences": cc,
                    "document_count": cd, "documents": cd},
        "before": {"count": oc, "occurrences": oc,
                   "document_count": od, "documents": od},
        "after": {"count": cc, "occurrences": cc,
                  "document_count": cd, "documents": cd},
    }


# ---------------------------------------------------------------- 内部

def _count_starts(text: str, term: str, lo: int, hi: int) -> int:
    """统计在 text 中起点落在 [lo, hi) 的出现次数；重叠出现分别计数。"""
    n = len(term)
    if n == 0:
        return 0
    lo = max(0, lo)
    hi = min(len(text) - n + 1, hi)
    if hi <= lo:
        return 0
    count = 0
    i = text.find(term, lo)
    while i != -1 and i < hi:
        count += 1
        i = text.find(term, i + 1)
    return count


def _apply_current_delta(conn: sqlite3.Connection, doc_id: int,
                         old_pieces: list[dict], new_pieces: list[dict],
                         original: str, old_texts: dict[int, str],
                         new_texts: dict[int, str],
                         old_span: tuple[int, int], new_span: tuple[int, int]) -> None:
    """把一笔改动对现稿词频的影响增量写入已保存统计。

    old_span/new_span 分别是改动前/后真正发生变化的区间。只取改动文字与
    可能跨过改动边界的上下文（各词组长度 - 1 个字符），不扫描全文。
    """
    terms = db.word_terms(conn)
    if not terms:
        return
    stats = db.word_stat_rows(conn, doc_id)
    old_len = engine.total_length(old_pieces)
    new_len = engine.total_length(new_pieces)
    os_, oe = old_span
    ns, ne = new_span
    for term in terms:
        L = len(term)
        if L == 0:
            continue
        oc, cc = stats.get(term, (0, 0))
        # 原稿不受勘误/撤回影响，原稿计数保持不变

        old_lo = os_ - (L - 1)
        old_hi = oe
        old_ctx_start = max(0, old_lo)
        old_ctx_end = min(old_len, oe + (L - 1))
        old_ctx = engine.render_slice(old_pieces, original, old_texts,
                                      old_ctx_start, old_ctx_end)
        old_hits = _count_starts(old_ctx, term,
                                 old_lo - old_ctx_start, old_hi - old_ctx_start)

        new_lo = ns - (L - 1)
        new_hi = ne
        new_ctx_start = max(0, new_lo)
        new_ctx_end = min(new_len, ne + (L - 1))
        new_ctx = engine.render_slice(new_pieces, original, new_texts,
                                      new_ctx_start, new_ctx_end)
        new_hits = _count_starts(new_ctx, term,
                                 new_lo - new_ctx_start, new_hi - new_ctx_start)

        stats[term] = (oc, max(0, cc + new_hits - old_hits))
    db.save_word_stats(conn, doc_id, stats)


def _recount_current_after_edit(conn: sqlite3.Connection, doc_id: int,
                                original: str, old_pieces: list[dict],
                                new_pieces: list[dict],
                                old_texts: dict[int, str],
                                old_span: tuple[int, int],
                                new_text_len: int) -> None:
    """把一笔改动对现稿统计的影响增量写入已保存统计。

    old_span 是改动前真正发生变化的区间，新区间从同一起点开始、长度为
    new_text_len。只重算改动文字及可能跨过改动边界的上下文。
    """
    s, e = old_span
    new_texts = db.correction_texts(conn, doc_id)
    _apply_current_delta(conn, doc_id, old_pieces, new_pieces, original,
                         old_texts, new_texts,
                         (s, e), (s, s + new_text_len))


def _get_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"文档不存在: {doc_id}")
    return row


def _entities(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT id, number, kind, name, start, "end" FROM entities'
        " WHERE document_id = ? ORDER BY number",
        (doc_id,),
    ).fetchall()


def _effects_map(conn: sqlite3.Connection, doc_id: int) -> dict[int, list[int]]:
    rows = conn.execute(
        "SELECT entity_id, correction_number FROM entity_effects"
        " WHERE document_id = ? ORDER BY correction_number",
        (doc_id,),
    ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append(r["correction_number"])
    return out


def _entity_view(pieces: list[dict], ent: sqlite3.Row,
                 effects: dict[int, list[int]]) -> dict:
    cs, ce = engine.current_range(pieces, ent["start"], ent["end"])
    affecting = effects.get(ent["id"], [])
    return {
        "number": ent["number"],
        "kind": ent["kind"],
        "name": ent["name"],
        "start": cs,
        "end": ce,
        "status": "pending_review" if affecting else "valid",
        "affecting_corrections": affecting,
        "original_start": ent["start"],
        "original_end": ent["end"],
    }


def _bump_revision(conn: sqlite3.Connection, doc_id: int) -> int:
    conn.execute("UPDATE documents SET revision = revision + 1 WHERE id = ?", (doc_id,))
    return conn.execute(
        "SELECT revision FROM documents WHERE id = ?", (doc_id,)).fetchone()["revision"]
