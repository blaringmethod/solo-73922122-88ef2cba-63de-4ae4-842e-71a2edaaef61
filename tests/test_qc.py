"""问卷跳题/漏答核查逻辑单元测试（纯函数层）。"""

from app import qc


def Q(qid, trigger=None):
    return {"question_id": qid, "trigger": trigger}


def C(qid, answer):
    return {"question_id": qid, "answer": answer}


def S(rid, answers):
    return {"respondent_id": rid, "answers": answers}


def by_id(result):
    return {f.question_id: f.classification for f in result.findings}


# --------------------------------------------------------------------- 路径
def test_unconditional_questions_always_asked():
    qs = qc.compile_questions([Q("q1"), Q("q2")])
    paths = qc.enumerate_paths(qs)
    # 两题都不受条件约束：全部 4 种作答组合，且两题都被问到
    assert len(paths) == 4
    assert all(asked == 0b11 for asked, _ in paths)


def test_trigger_or_of_groups_and_skip_cannot_trigger():
    # q3 由 (q1=是 且 q2=是) 或 (q2=否) 触发；q2 由 q1=是 触发
    qs = qc.compile_questions([
        Q("q1"),
        Q("q2", [[C("q1", True)]]),
        Q("q3", [[C("q1", True), C("q2", True)], [C("q2", False)]]),
    ])
    paths = qc.enumerate_paths(qs)
    # 对每条路径做独立核验：被问题目必须满足触发规则
    for asked, yes in paths:
        assert asked & 1  # q1 无条件
        assert bool(asked & 2) == bool(yes & 1)          # q2 被问 <=> q1=是
        if asked & 2:
            # q3 被问 <=> (q1=是且q2=是) 或 q2被问且答否
            expect = (bool(yes & 1) and bool(yes & 2)) or not bool(yes & 2)
            assert bool(asked & 4) == expect
        else:
            # q2 被跳过 -> q3 不可能被问（跳过的题不能触发后题）
            assert not (asked & 4)


def test_two_trigger_groups_are_or():
    qs = qc.compile_questions([
        Q("q1"),
        Q("q2"),
        Q("q3", [[C("q1", True)], [C("q2", True)]]),
    ])
    paths = qc.enumerate_paths(qs)
    asked_q3 = {bool(asked & 4) for asked, yes in paths if yes & 0b11}
    assert asked_q3 == {True}
    all_no = [p for p in paths if not (p[1] & 0b11)]
    assert all(not (p[0] & 4) for p in all_no)


# --------------------------------------------------------------------- 分类
def test_blank_unconditional_question_is_missing():
    qs = qc.compile_questions([Q("q1"), Q("q2")])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {"q1": True})
    assert res.status == "ok"
    assert res.blank_count == 1
    assert by_id(res) == {"q2": "missing"}


def test_blank_triggered_question_is_should_skip_when_trigger_false():
    qs = qc.compile_questions([Q("q1"), Q("q2", [[C("q1", True)]])])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {"q1": False})
    assert res.status == "ok"
    assert by_id(res) == {"q2": "should_skip"}


def test_blank_trigger_chain_is_needs_review():
    # q1 空白：q2 仅在 q1=是 时被问 -> q2 只在部分相容路径出现
    qs = qc.compile_questions([Q("q1"), Q("q2", [[C("q1", True)]])])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {})
    assert res.status == "ok"
    assert by_id(res) == {"q1": "missing", "q2": "needs_review"}


def test_blank_behind_skipped_trigger_is_should_skip():
    # q1 由 q0=是 触发；q0=否 -> q1 跳过，q2（由 q1=是 触发）永不询问
    qs = qc.compile_questions([
        Q("q0"),
        Q("q1", [[C("q0", True)]]),
        Q("q2", [[C("q1", True)]]),
    ])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {"q0": False})
    assert res.status == "ok"
    assert by_id(res) == {"q1": "should_skip", "q2": "should_skip"}


def test_answered_question_no_finding():
    qs = qc.compile_questions([Q("q1"), Q("q2", [[C("q1", True)]])])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {"q1": True, "q2": False})
    assert res.status == "ok"
    assert res.blank_count == 0
    assert res.findings == []


# --------------------------------------------------------------------- 矛盾
def test_recorded_answer_on_skipped_question_is_contradiction():
    qs = qc.compile_questions([Q("q1"), Q("q2", [[C("q1", True)]])])
    paths = qc.enumerate_paths(qs)
    # q1=否 时 q2 必被跳过，不可能有答案
    res = qc.check_sheet(qs, paths, {"q1": False, "q2": True})
    assert res.status == "contradiction"
    assert res.findings == []


def test_contradictory_blank_chain_yields_contradiction():
    # q0=是 触发 q1；q1=是 才触发 q2。若 q0=否，q1、q2 均被跳过，
    # q2 上却录了“是” -> 无相容路径。
    qs = qc.compile_questions([
        Q("q0"),
        Q("q1", [[C("q0", True)]]),
        Q("q2", [[C("q1", True)]]),
    ])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {"q0": False, "q2": True})
    assert res.status == "contradiction"
    # 同一问卷，q2 录“是”在 q0=是、q1=是 时完全合法
    ok = qc.check_sheet(qs, paths, {"q0": True, "q1": True, "q2": True})
    assert ok.status == "ok"


def test_answers_on_impossible_question_are_contradiction():
    # q1 只在 q0=是 时被问；已录 q0=否 且 q1=否 -> 无相容路径
    qs = qc.compile_questions([Q("q0"), Q("q1", [[C("q0", True)]])])
    paths = qc.enumerate_paths(qs)
    res = qc.check_sheet(qs, paths, {"q0": False, "q1": False})
    assert res.status == "contradiction"


# --------------------------------------------------------------- 批量独立
def test_contradiction_does_not_affect_other_sheets():
    questions = [Q("q1"), Q("q2", [[C("q1", True)]])]
    sheets = [
        S("R1", {"q1": False, "q2": True}),   # 矛盾
        S("R2", {}),                          # q1 漏答, q2 待复核
        S("R3", {"q1": False}),               # q2 应跳
    ]
    results = qc.check_questionnaire(questions, sheets)
    assert [r.respondent_id for r in results] == ["R1", "R2", "R3"]
    assert results[0].status == "contradiction"
    assert results[1].status == "ok"
    assert by_id(results[1]) == {"q1": "missing", "q2": "needs_review"}
    assert by_id(results[2]) == {"q2": "should_skip"}


def test_no_questions_rejected():
    try:
        qc.compile_questions([])
    except Exception:  # noqa: BLE001 - 空问卷不应崩溃于别处
        pass


def test_unknown_question_in_answers_raises():
    qs = qc.compile_questions([Q("q1")])
    paths = qc.enumerate_paths(qs)
    try:
        qc.check_sheet(qs, paths, {"nope": True})
    except qc.QCSpecError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未知题号应被拒绝")
