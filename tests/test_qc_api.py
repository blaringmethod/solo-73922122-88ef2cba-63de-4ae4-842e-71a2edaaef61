"""问卷质控 API 测试：版本/样本成员校验、409 语义、批量独立、文档可见。"""

import importlib
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIGHTS_DB", str(tmp_path / "qc.db"))
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c, main
    main.app.state.jobs.shutdown()


SAMPLES = [
    {"person_id": "R1", "region": "E", "age_group": "y", "d": 1},
    {"person_id": "R2", "region": "E", "age_group": "m", "d": 1},
    {"person_id": "R3", "region": "W", "age_group": "y", "d": 1},
]

QUESTIONS = [
    {"question_id": "q1"},
    {"question_id": "q2", "trigger": [[
        {"question_id": "q1", "answer": True}]]},
    {"question_id": "q3"},
]


def setup(c):
    assert c.put("/samples", json={"samples": SAMPLES}).status_code == 200
    assert c.put("/targets", json={"targets": []}).status_code == 200
    return c.get("/health").json()["epoch"]


def post(c, body):
    return c.post("/qc/questionnaire", json=body)


def test_qc_classifies_blank_questions(client):
    c, _ = client
    version = setup(c)
    r = post(c, {
        "version_id": version,
        "questions": QUESTIONS,
        "sheets": [
            {"respondent_id": "R1", "answers": {"q1": False, "q3": True}},
            {"respondent_id": "R2", "answers": {"q1": True}},
            {"respondent_id": "R3", "answers": {"q3": False}},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["version_id"] == version
    sheets = {s["respondent_id"]: s for s in body["sheets"]}
    assert sheets["R1"]["status"] == "ok"
    assert {f["question_id"]: f["classification"]
            for f in sheets["R1"]["findings"]} == {"q2": "should_skip"}
    assert sheets["R1"]["findings"][0]["label"] == "应跳"
    assert {f["question_id"]: f["classification"]
            for f in sheets["R2"]["findings"]} == {
                "q2": "missing", "q3": "missing"}
    assert {f["question_id"]: f["classification"]
            for f in sheets["R3"]["findings"]} == {
                "q1": "missing", "q2": "needs_review"}
    assert sheets["R3"]["findings"][0]["position"] == 1
    assert sheets["R3"]["findings"][1]["label"] == "待复核"
    assert body["summary"] == {
        "sheets": 3, "ok": 3, "contradiction": 0,
        "missing": 3, "should_skip": 1, "needs_review": 1,
    }
    # 质控是只读核查：不改变版本
    assert c.get("/health").json()["epoch"] == version


def test_qc_contradiction_marked_but_others_checked(client):
    c, _ = client
    version = setup(c)
    r = post(c, {
        "version_id": version,
        "questions": QUESTIONS[:2],
        "sheets": [
            {"respondent_id": "R1", "answers": {"q1": False, "q2": True}},
            {"respondent_id": "R2", "answers": {"q1": False}},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    first, second = body["sheets"]
    assert first["status"] == "contradiction"
    assert first["status_label"] == "矛盾"
    assert first["findings"] == []
    assert first["blank_count"] == 0
    assert second["status"] == "ok"
    assert second["findings"][0]["classification"] == "should_skip"
    assert body["summary"]["contradiction"] == 1
    assert body["summary"]["should_skip"] == 1


def test_qc_stale_version_returns_409(client):
    c, _ = client
    version = setup(c)
    assert c.put("/samples", json={"samples": SAMPLES}).status_code == 200
    r = post(c, {"version_id": version, "questions": QUESTIONS[:1],
                 "sheets": [{"respondent_id": "R1", "answers": {}}]})
    assert r.status_code == 409
    assert "current_version_id" in r.json()


def test_qc_unknown_respondent_422(client):
    c, _ = client
    version = setup(c)
    r = post(c, {"version_id": version, "questions": QUESTIONS[:1],
                 "sheets": [{"respondent_id": "GHOST", "answers": {}}]})
    assert r.status_code == 422
    assert "GHOST" in r.json()["detail"]
    # 未知受访者整批拒绝，不会部分核查
    r = post(c, {"version_id": version, "questions": QUESTIONS[:1],
                 "sheets": [{"respondent_id": "R1", "answers": {}},
                            {"respondent_id": "GHOST", "answers": {}}]})
    assert r.status_code == 422


def test_qc_duplicate_respondent_422(client):
    c, _ = client
    version = setup(c)
    r = post(c, {"version_id": version, "questions": QUESTIONS[:1],
                 "sheets": [{"respondent_id": "R1", "answers": {}},
                            {"respondent_id": "R1", "answers": {}}]})
    assert r.status_code == 422


def test_qc_sheet_validation(client):
    c, _ = client
    version = setup(c)
    common = {"version_id": version, "questions": QUESTIONS[:1],
              "sheets": [{"respondent_id": "R1", "answers": {}}]}
    # 布尔答案必须是真正的布尔值
    bad = {**common, "sheets": [{"respondent_id": "R1",
                                 "answers": {"q1": 1}}]}
    assert post(c, bad).status_code == 422
    # 未知题目
    bad = {**common, "sheets": [{"respondent_id": "R1",
                                 "answers": {"nope": True}}]}
    assert post(c, bad).status_code == 422
    # 额外字段
    bad = {**common, "sheets": [{"respondent_id": "R1", "answers": {},
                                 "extra": 1}]}
    assert post(c, bad).status_code == 422


def test_qc_question_validation(client):
    c, _ = client
    version = setup(c)
    base = {"version_id": version,
            "sheets": [{"respondent_id": "R1", "answers": {}}]}
    # 空问卷合法（至多 12 道），无题可空白
    r = post(c, {**base, "questions": []})
    assert r.status_code == 200
    assert r.json()["sheets"][0]["findings"] == []
    # 但答卷不能含空问卷里不存在的题号
    r = post(c, {**base, "questions": [],
                 "sheets": [{"respondent_id": "R1",
                             "answers": {"q1": True}}]})
    assert r.status_code == 422
    # 超过 12 题
    many = [{"question_id": f"q{i}"} for i in range(13)]
    assert post(c, {**base, "questions": many}).status_code == 422
    # 重复题号
    assert post(c, {**base, "questions": [
        {"question_id": "q1"}, {"question_id": "q1"}]}).status_code == 422
    # 引用后题
    assert post(c, {**base, "questions": [
        {"question_id": "q1", "trigger": [[
            {"question_id": "q2", "answer": True}]]},
        {"question_id": "q2"}]}).status_code == 422
    # 引用自身
    assert post(c, {**base, "questions": [
        {"question_id": "q1", "trigger": [[
            {"question_id": "q1", "answer": True}]]}]}).status_code == 422
    # 空触发组
    assert post(c, {**base, "questions": [
        {"question_id": "q1"}, {"question_id": "q2", "trigger": [[]]}]}
    ).status_code == 422
    # 版本号必须是整数
    assert post(c, {"version_id": "1", "questions": QUESTIONS[:1],
                    "sheets": []}).status_code == 422
    # 允许空答卷批次
    assert post(c, {"version_id": version, "questions": QUESTIONS[:1],
                    "sheets": []}).status_code == 200


def test_qc_is_documented(client):
    c, _ = client
    spec = c.get("/openapi.json").json()
    assert "/qc/questionnaire" in spec["paths"]
    body = spec["paths"]["/qc/questionnaire"]["post"]
    assert "问卷质控" in body["tags"]
    assert c.get("/docs").status_code == 200


def test_qc_during_input_update_returns_409_minus_race(client, monkeypatch):
    """核查期间样本/目标被更正 -> 整批 409；未变更则正常返回。

    构造方式：在纯逻辑核查入口注入“提交更正”的动作，模拟并发窗口内
    版本号在读取快照之后、返回之前发生变化。
    """
    c, main = client
    version = setup(c)

    import app.qc as qc_mod
    orig = qc_mod.check_questionnaire
    fired = {"done": False}

    def mutate_then_check(questions, sheets):
        if not fired["done"]:
            fired["done"] = True
            # 模拟另一名分析员在核查进行中更正了人口目标
            main.app.state.db.replace_targets([])
        return orig(questions, sheets)

    monkeypatch.setattr(main, "check_questionnaire", mutate_then_check)
    r = post(c, {"version_id": version, "questions": QUESTIONS[:1],
                 "sheets": [{"respondent_id": "R1", "answers": {}}]})
    assert r.status_code == 409
    assert r.json()["current_version_id"] == version + 1
    # 最新版本可正常重核
    r2 = post(c, {"version_id": version + 1, "questions": QUESTIONS[:1],
                  "sheets": [{"respondent_id": "R1", "answers": {}}]})
    assert r2.status_code == 200
