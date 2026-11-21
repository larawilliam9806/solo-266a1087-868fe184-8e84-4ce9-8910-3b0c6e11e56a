"""口述史转录稿勘误供数 API（FastAPI + SQLite）。

- POST   /documents                                  导入正文与实体标注
- GET    /documents                                  文档列表
- GET    /documents/{doc_id}                         版本查询：正文 + 有效标注区间 + 待复核标注
- POST   /documents/{doc_id}/corrections             应用一笔勘误，返回勘误号
- POST   /documents/{doc_id}/corrections/{n}/retract 按号撤回（冲突则 409 并保留现版）
- GET    /documents/{doc_id}/corrections             勘误流水
- GET    /documents/{doc_id}/entities                实体检索（kind / q 过滤）
- GET    /documents/{doc_id}/entities/{number}       单个实体
- POST   /wordstats/initialize                       从本地词表启用专题词频统计
- GET    /wordstats                                  查询已保存的词频统计（?term= 过滤）
- GET    /wordstats/terms/{term}                     查询单个词组
- GET    /documents/{doc_id}/wordstats               单篇稿件的词组统计
- GET    /word-frequency 系列                        以上词频接口的别名
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import db, service


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 首次启动时若配置了本地词表则启用统计并纳入已有稿件；
    # 之后重启沿用库中保存的词表，不重复累计。
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        service.initialize_word_stats_from_env(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    yield


app = FastAPI(title="口述史转录稿勘误供数 API", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 事务辅助

def _write(fn, *args):
    """写操作：BEGIN IMMEDIATE 保证读写串行，异常回滚。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = fn(conn, *args)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read(fn, *args):
    """读操作：显式读事务，保证同一请求内多次查询落在同一版本快照上。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN DEFERRED")
        return fn(conn, *args)
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------- 模型

class EntityIn(BaseModel):
    number: Optional[int] = Field(default=None, description="实体编号，缺省自动分配")
    kind: Literal["person", "place"]
    name: str = Field(min_length=1)
    start: int = Field(ge=0, description="原文区间起点（含）")
    end: int = Field(ge=0, description="原文区间终点（不含）")


class ImportIn(BaseModel):
    text: str = Field(description="转录稿正文")
    entities: list[EntityIn] = Field(default_factory=list)


class CorrectionIn(BaseModel):
    start: int = Field(ge=0, description="当前版区间起点（含）")
    end: int = Field(ge=0, description="当前版区间终点（不含）")
    text: str = Field(default="", description="替换文本；区间为空即插入，文本为空即删除")


class WordStatsInitIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    terms: Optional[list[str]] = Field(
        default=None, description="词表词组；缺省读取本地词表文件/环境变量")
    path: Optional[str] = Field(default=None, description="本地词表文件路径")
    replace: bool = Field(
        default=False, description="整体替换已保存的词表并重算已有稿件")

    def term_list(self) -> Optional[list[str]]:
        """接受 terms / wordlist / words / 单个 term 等常见写法。"""
        if self.terms is not None:
            return self.terms
        extra = self.model_extra or {}
        for key in ("wordlist", "words", "term_list", "terms_list", "term"):
            value = extra.get(key)
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [str(v) for v in value]
        return None


# ---------------------------------------------------------------- 异常映射

@app.exception_handler(service.NotFoundError)
async def not_found_handler(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.ValidationError)
async def validation_handler(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(service.ConflictError)
async def conflict_handler(request, exc: service.ConflictError):
    # 撤回被冲突阻断：返回冲突勘误号，现版保持不变
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "conflicts": exc.conflicts},
    )


# ---------------------------------------------------------------- 路由

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/documents", status_code=201)
def import_document(payload: ImportIn):
    return _write(service.import_document, payload.text,
                  [e.model_dump() for e in payload.entities])


@app.get("/documents")
def documents():
    return _read(service.list_documents)


@app.get("/documents/{doc_id}")
def get_version(doc_id: int):
    """版本查询：同一版返回正文、有效标注当前区间、待复核标注。"""
    return _read(service.get_version, doc_id)


@app.post("/documents/{doc_id}/corrections", status_code=201)
def apply_correction(doc_id: int, payload: CorrectionIn):
    return _write(service.apply_correction, doc_id,
                  payload.start, payload.end, payload.text)


@app.post("/documents/{doc_id}/corrections/{number}/retract")
def retract_correction(doc_id: int, number: int):
    return _write(service.retract_correction, doc_id, number)


@app.get("/documents/{doc_id}/corrections")
def corrections(doc_id: int):
    return _read(service.list_corrections, doc_id)


@app.get("/documents/{doc_id}/entities")
def entities(doc_id: int, kind: Optional[Literal["person", "place"]] = None,
             q: Optional[str] = None):
    return _read(service.search_entities, doc_id, kind, q)


@app.get("/documents/{doc_id}/entities/{number}")
def entity(doc_id: int, number: int):
    items = _read(service.search_entities, doc_id, None, None)
    for item in items:
        if item["number"] == number:
            return item
    raise HTTPException(status_code=404, detail=f"实体编号不存在: {number}")


# ---------------------------------------------------------------- 专题词频

@app.post("/wordstats")
@app.post("/wordstats/initialize")
def initialize_word_stats(payload: Optional[WordStatsInitIn] = None):
    """从本地词表启用/更新统计（首次启用会纳入全部已有稿件）。"""
    payload = payload or WordStatsInitIn()
    return _write(service.initialize_word_stats,
                  payload.term_list(), payload.replace, payload.path)


@app.get("/wordstats")
def word_stats(request: Request,
               term: Optional[str] = None,
               terms: Optional[str] = None,
               q: Optional[str] = None,
               query: Optional[str] = None,
               word: Optional[str] = None):
    """查询词表中词组的原稿/现稿总出现次数与涉及稿数。

    过滤参数：`term`（可重复传入多个）/ `q` / `word` / `query`（单个词组）/
    `terms`（逗号分隔）。缺省返回整个词表。
    查询只读已保存的统计，不重新扫描正文。
    """
    wanted: list[str] = list(request.query_params.getlist("term"))
    for single in (q, query, word):
        if single:
            wanted.append(single)
    if terms:
        wanted += [t.strip() for t in terms.replace("，", ",").split(",") if t.strip()]
    return _read(service.list_word_stats, wanted or None)


@app.get("/wordstats/terms/{term}")
def word_stat_term(term: str):
    return _read(service.get_word_stat, term)


# 兼容别名：/wordstats/{term} 与 /word-frequency 系列
@app.post("/word-frequency/initialize")
def initialize_word_frequency(payload: Optional[WordStatsInitIn] = None):
    return initialize_word_stats(payload)


@app.get("/word-frequency")
def word_frequency(request: Request,
                   term: Optional[str] = None,
                   terms: Optional[str] = None,
                   q: Optional[str] = None,
                   query: Optional[str] = None,
                   word: Optional[str] = None):
    return word_stats(request, term, terms, q, query, word)


@app.get("/word-frequency/{term}")
def word_frequency_term(term: str):
    return _read(service.get_word_stat, term)


# 兼容别名：/wordstats/query?term=... 与 /wordstats/search?term=...
@app.get("/wordstats/query")
def word_stats_query(request: Request,
                     term: Optional[str] = None,
                     terms: Optional[str] = None,
                     q: Optional[str] = None):
    return word_stats(request, term, terms, q)


@app.get("/wordstats/search")
def word_stats_search(request: Request,
                      term: Optional[str] = None,
                      terms: Optional[str] = None,
                      q: Optional[str] = None):
    return word_stats(request, term, terms, q)


@app.get("/documents/{doc_id}/wordstats")
def document_word_stats(doc_id: int):
    return _read(service.document_word_stats, doc_id)


@app.get("/documents/{doc_id}/wordstats/{term}")
def document_word_stat(doc_id: int, term: str):
    data = _read(service.document_word_stats, doc_id)
    for item in data["terms"]:
        if item["term"] == term:
            return item
    raise HTTPException(status_code=404, detail=f"词组不在词表中: {term}")
