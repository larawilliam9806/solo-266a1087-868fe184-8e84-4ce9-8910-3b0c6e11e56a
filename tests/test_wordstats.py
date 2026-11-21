"""专题词频 API 集成测试。"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_client(tmp_path, monkeypatch, wordlist=None, env_terms=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)   # 避免读到仓库根目录的默认词表
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.delenv("WORDLIST_PATH", raising=False)
    monkeypatch.delenv("WORD_LIST_PATH", raising=False)
    monkeypatch.delenv("WORDLIST_FILE", raising=False)
    if env_terms is not None:
        monkeypatch.setenv("WORDLIST_TERMS", env_terms)
    else:
        monkeypatch.delenv("WORDLIST_TERMS", raising=False)
    if wordlist is not None:
        path = tmp_path / "wordlist.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wordlist, encoding="utf-8")
        monkeypatch.setenv("WORDLIST_PATH", str(path))
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app), db_path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, wordlist="北京\n胡同\n")
    with c:
        yield c


def import_doc(client, text, entities=None):
    r = client.post("/documents", json={"text": text, "entities": entities or []})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def correct(client, doc, start, end, text=""):
    r = client.post(f"/documents/{doc}/corrections",
                    json={"start": start, "end": end, "text": text})
    assert r.status_code == 201, r.text
    return r.json()["correction_number"]


def retract(client, doc, n):
    return client.post(f"/documents/{doc}/corrections/{n}/retract")


def stats(client, term):
    r = client.get(f"/wordstats/terms/{term}")
    assert r.status_code == 200, r.text
    return r.json()


def all_stats(client):
    r = client.get("/wordstats")
    assert r.status_code == 200, r.text
    return {x["term"]: x for x in r.json()}


# ---------------------------------------------------------------- 启用与回填

def test_enable_backfills_existing_documents(client):
    import_doc(client, "北京胡同北京")
    import_doc(client, "上海胡同")
    data = all_stats(client)
    assert data["北京"]["original_count"] == 2
    assert data["北京"]["current_count"] == 2
    assert data["北京"]["original_document_count"] == 1
    assert data["北京"]["current_document_count"] == 1
    assert data["胡同"]["original_count"] == 2
    assert data["胡同"]["original_document_count"] == 2
    assert data["北京"]["original"] == {
        "count": 2, "occurrences": 2, "document_count": 1, "documents": 1}
    assert data["北京"]["current"] == data["北京"]["original"]


def test_enable_via_endpoint_terms(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch)
    with c:
        import_doc(c, "沿海开放")
        r = c.post("/wordstats/initialize", json={"terms": ["开放", "沿海"]})
        assert r.status_code == 200, r.text
        assert r.json()["term_count"] == 2
        assert stats(c, "开放")["original_count"] == 1
        assert stats(c, "沿海")["current_count"] == 1


def test_restart_reuses_saved_wordlist(tmp_path, monkeypatch):
    # 第一次启动：从本地词表启用，并纳入已有稿件
    c1, db_path = make_client(tmp_path, monkeypatch, wordlist="北京\n")
    with c1:
        import_doc(c1, "北京")
        assert stats(c1, "北京")["original_count"] == 1

    # 本地文件换掉后重启：沿用库中词表，不再读文件
    path = tmp_path / "wordlist.txt"
    path.write_text("上海\n", encoding="utf-8")
    monkeypatch.setenv("WORDLIST_PATH", str(path))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c2:
        data = all_stats(c2)
        assert list(data) == ["北京"]
        assert c2.get("/wordstats/terms/上海").status_code == 404
        # 累计值没有被重启重复累加
        assert data["北京"]["original_count"] == 1


def test_wordlist_formats(tmp_path, monkeypatch):
    # 每行一个词组的纯文本（# 注释忽略），以及 JSON 数组
    c, _ = make_client(tmp_path, monkeypatch,
                       wordlist="# 专题\n江、河,湖;海\n")
    with c:
        assert set(all_stats(c)) == {"江", "河", "湖", "海"}

    c2, _ = make_client(tmp_path / "second", monkeypatch,
                        wordlist='["开放", "改革", "开放"]')
    with c2:
        assert list(all_stats(c2)) == ["开放", "改革"]


# ---------------------------------------------------------------- 查询语义

def test_new_import_counts_in_both_sets(client):
    import_doc(client, "北京")
    import_doc(client, "北京北京")
    data = all_stats(client)
    assert data["北京"]["original_count"] == 3
    assert data["北京"]["current_count"] == 3
    assert data["北京"]["original_document_count"] == 2
    assert data["北京"]["current_document_count"] == 2


def test_overlapping_occurrences_count_separately(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, wordlist="aaa\n")
    with c:
        import_doc(c, "aaaa")
        assert stats(c, "aaa")["original_count"] == 2
        assert stats(c, "aaa")["current_count"] == 2


def test_exact_character_match(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, wordlist="北京\n北京市\n")
    with c:
        import_doc(c, "北京市的北京人")   # “北京”2 处、“北京市”1 处（逐字子串）
        assert stats(c, "北京")["original_count"] == 2
        assert stats(c, "北京市")["original_count"] == 1
        assert c.get("/wordstats/terms/京").status_code == 404  # 未加入词表不可查


def test_query_reads_saved_stats_only(client, tmp_path):
    import_doc(client, "北京")
    assert stats(client, "北京")["original_count"] == 1
    db_file = os.environ["DB_PATH"]
    conn = sqlite3.connect(db_file)
    conn.execute("UPDATE word_stats SET original_count = 99, current_count = 98"
                 " WHERE term = '北京'")
    conn.commit()
    conn.close()
    data = stats(client, "北京")
    assert (data["original_count"], data["current_count"]) == (99, 98)


def test_query_filter_by_term(client):
    import_doc(client, "北京胡同")
    r = client.get("/wordstats", params={"term": "北京"})
    assert [x["term"] for x in r.json()] == ["北京"]
    r = client.get("/wordstats", params=[("term", "北京"), ("term", "胡同")])
    assert [x["term"] for x in r.json()] == ["北京", "胡同"]
    r = client.get("/wordstats", params={"q": "胡同"})
    assert [x["term"] for x in r.json()] == ["胡同"]
    assert client.get("/wordstats/terms/没有").status_code == 404


# ---------------------------------------------------------------- 勘误与撤回

def test_correction_updates_only_current(client):
    doc = import_doc(client, "北京胡同")
    correct(client, doc, 0, 2, "上海")       # 北京 -> 上海
    data = all_stats(client)
    assert data["北京"]["original_count"] == 1
    assert data["北京"]["current_count"] == 0
    assert data["北京"]["original_document_count"] == 1
    assert data["北京"]["current_document_count"] == 0
    assert data["胡同"]["current_count"] == 1


def test_retract_restores_current_counts(client):
    doc = import_doc(client, "北京胡同")
    n = correct(client, doc, 0, 2, "上海")
    assert stats(client, "北京")["current_count"] == 0
    assert retract(client, doc, n).status_code == 200
    assert stats(client, "北京")["current_count"] == 1
    assert client.get("/wordstats/terms/上海").status_code == 404  # 词表外不可查


def test_delete_and_reinsert_across_boundaries(client):
    doc = import_doc(client, "北京胡同")
    # 删掉“京胡”中间处的“京胡”之交，制造跨词组边界的变化
    n = correct(client, doc, 1, 3)           # “京胡” -> 删
    assert stats(client, "北京")["current_count"] == 0
    assert stats(client, "胡同")["current_count"] == 0
    retract(client, doc, n)
    assert stats(client, "北京")["current_count"] == 1
    assert stats(client, "胡同")["current_count"] == 1


def test_insert_before_creates_new_occurrence(client):
    doc = import_doc(client, "京")
    correct(client, doc, 0, 0, "北")
    assert stats(client, "北京")["current_count"] == 1
    assert stats(client, "北京")["original_count"] == 0


def test_two_documents_document_counts(client):
    d1 = import_doc(client, "北京胡同")
    d2 = import_doc(client, "北京")
    correct(client, d2, 0, 2, "上海")        # 第二份现稿不再含“北京”
    data = stats(client, "北京")
    assert data["original_count"] == 2
    assert data["current_count"] == 1
    assert data["original_document_count"] == 2
    assert data["current_document_count"] == 1
    assert data["current"]["document_count"] == 1
    assert d1 != d2


def test_retract_after_earlier_correction_moved_it(client):
    """前文勘误使撤回位置移动时，按实际还原的位置更新统计。"""
    doc = import_doc(client, "北京胡同")
    n_hutong = correct(client, doc, 2, 4, "巷子")   # 1: 北京巷子
    assert stats(client, "胡同")["current_count"] == 0
    # 在文首插入，使勘误 1 的落点后移
    correct(client, doc, 0, 0, "老")                 # 2: 老北京巷子
    assert stats(client, "胡同")["current_count"] == 0
    # 撤回 1，位置已移动，但应准确恢复“胡同”
    assert retract(client, doc, n_hutong).status_code == 200
    assert stats(client, "胡同")["current_count"] == 1
    assert stats(client, "北京")["current_count"] == 1
    assert client.get(f"/documents/{doc}").json()["text"] == "老北京胡同"


def test_failed_write_leaves_text_and_stats_unchanged(client):
    doc = import_doc(client, "北京")
    before = stats(client, "北京")
    r = client.post(f"/documents/{doc}/corrections",
                    json={"start": 0, "end": 99, "text": "上海"})
    assert r.status_code == 400
    assert client.get(f"/documents/{doc}").json()["text"] == "北京"
    assert stats(client, "北京") == before
    # 空操作同样不改变任何统计
    r = client.post(f"/documents/{doc}/corrections",
                    json={"start": 1, "end": 1, "text": ""})
    assert r.status_code == 400
    assert stats(client, "北京") == before


def test_conflicting_retract_keeps_stats(client):
    doc = import_doc(client, "北京胡同")
    correct(client, doc, 0, 2, "上海")        # 1
    correct(client, doc, 0, 1, "津")          # 2: 改掉勘误 1 写入的“上”
    before = all_stats(client)
    assert retract(client, doc, 1).status_code == 409
    assert all_stats(client) == before


def test_document_word_stats(client):
    doc = import_doc(client, "北京北京胡同")
    r = client.get(f"/documents/{doc}/wordstats")
    assert r.status_code == 200, r.text
    items = {x["term"]: x for x in r.json()["terms"]}
    assert items["北京"] == {"term": "北京", "original_count": 2,
                             "current_count": 2}
    assert client.get(f"/documents/{doc}/wordstats/胡同").json()["current_count"] == 1
    assert client.get(f"/documents/{doc}/wordstats/没有").status_code == 404
    assert client.get("/documents/999/wordstats").status_code == 404


# ---------------------------------------------------------------- 环境词表

def test_env_terms_initialize(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, env_terms="开放,改革")
    with c:
        import_doc(c, "改革开放")
        data = all_stats(c)
        assert set(data) == {"开放", "改革"}
        assert data["开放"]["original_count"] == 1
        assert data["改革"]["current_count"] == 1


def test_atomicity_stats_use_same_transaction(tmp_path, monkeypatch):
    """统计与正文同事务：写入失败时正文与计数都保持原样。"""
    c, db_path = make_client(tmp_path, monkeypatch, wordlist="北京\n")
    with c:
        doc = import_doc(c, "北京")
        before_text = c.get(f"/documents/{doc}").json()["text"]
        before = all_stats(c)
        r = c.post(f"/documents/{doc}/corrections",
                   json={"start": 0, "end": 5, "text": "上海"})
        assert r.status_code == 400
        assert c.get(f"/documents/{doc}").json()["text"] == before_text
        assert all_stats(c) == before


def test_initialize_endpoint_accepts_aliases(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch)
    with c:
        import_doc(c, "北京")
        r = c.post("/wordstats/initialize", json={"wordlist": ["北京"]})
        assert r.status_code == 200, r.text
        assert r.json()["term_count"] == 1
        assert stats(c, "北京")["original_count"] == 1


def test_initialize_replace_wordlist(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, wordlist="北京\n")
    with c:
        import_doc(c, "北京胡同")
        assert set(all_stats(c)) == {"北京"}
        r = c.post("/wordstats/initialize",
                   json={"terms": ["胡同"], "replace": True})
        assert r.status_code == 200, r.text
        assert set(all_stats(c)) == {"胡同"}
        assert stats(c, "胡同")["original_count"] == 1
        assert c.get("/wordstats/terms/北京").status_code == 404


def test_delete_retract_after_earlier_insert_shifts_seam(client):
    """前文插入把纯删除的接缝推到后面，撤回时按实际位置还原统计。"""
    doc = import_doc(client, "北京胡同")
    # 纯删除“胡”，留下接缝：北京同
    n = correct(client, doc, 2, 3)
    assert client.get(f"/documents/{doc}").json()["text"] == "北京同"
    assert stats(client, "胡同")["current_count"] == 0
    assert stats(client, "北京")["current_count"] == 1
    # 在更前面插入，接缝位置后移
    correct(client, doc, 0, 0, "老")
    assert client.get(f"/documents/{doc}").json()["text"] == "老北京同"
    # 撤回纯删除：按实际接缝位置还原“胡”，应重新形成“胡同”
    assert retract(client, doc, n).status_code == 200
    assert client.get(f"/documents/{doc}").json()["text"] == "老北京胡同"
    assert stats(client, "胡同")["current_count"] == 1
    assert stats(client, "胡同")["original_count"] == 1
