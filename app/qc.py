"""回访问卷跳题逻辑与漏答核查（纯函数，不依赖 Web/数据库）。

模型
----
- 问卷是至多 12 道按序排列的是非题；
- 题目可以无条件询问（``trigger is None``），也可以由若干“触发组”触发：
  任一触发组的全部条件都满足即询问该题；组内是“与”，组间是“或”；
- 条件只能引用更早的题；被跳过的题没有答案，因此永远无法满足任何条件
  （“跳过的题不能触发后题”）；
- 一条合法作答路径只给实际被问到的题分配“是/否”，未被问到的题没有答案；
- 路径与已录答卷相容，当且仅当：已录答案所在题在路径中确实被问到，
  且答案一致。已录答案出现在路径中应跳过的题上即为不相容。

空白题分类（在该答卷的全部相容路径上考察）
------------------------------------------
- 总是出现 -> ``missing``（漏答）；
- 从未出现 -> ``should_skip``（应跳）；
- 只在部分路径出现 -> ``needs_review``（待复核）。

若没有任何相容路径，该答卷为 ``contradiction``（矛盾），不做逐题分类；
同批其他答卷照常核查。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_QUESTIONS = 12

CLASSIFICATION_LABELS = {
    "missing": "漏答",
    "should_skip": "应跳",
    "needs_review": "待复核",
}
STATUS_LABELS = {"ok": "正常", "contradiction": "矛盾"}


class QCSpecError(ValueError):
    """问卷定义不合法（API 层另有 Pydantic 校验，此处为防御性检查）。"""


@dataclass(frozen=True)
class Condition:
    index: int          # 被引用的前题下标
    expected: bool      # 前题须给出的答案


@dataclass(frozen=True)
class Question:
    question_id: str
    # None 表示无条件询问；否则为触发组（组间“或”，组内“与”）
    trigger: tuple[tuple[Condition, ...], ...] | None


@dataclass(frozen=True)
class Finding:
    question_id: str
    position: int                 # 1 起
    classification: str           # missing | should_skip | needs_review


@dataclass
class SheetResult:
    respondent_id: str
    status: str                   # ok | contradiction
    blank_count: int
    findings: list[Finding]


def compile_questions(raw_questions: list[dict]) -> list[Question]:
    """把接口层的题目定义编译为下标化的条件（引用必须指向更早的题）。"""
    seen: dict[str, int] = {}
    compiled: list[Question] = []
    for i, raw in enumerate(raw_questions):
        qid = raw["question_id"].strip()
        if qid in seen:
            raise QCSpecError(f"question_id 重复: {qid}")
        trigger_raw = raw.get("trigger")
        trigger: tuple[tuple[Condition, ...], ...] | None = None
        if trigger_raw is not None:
            if not trigger_raw:
                raise QCSpecError(f"题目 {qid} 的 trigger 须至少包含一组条件")
            groups: list[tuple[Condition, ...]] = []
            for group in trigger_raw:
                if not group:
                    raise QCSpecError(f"题目 {qid} 的触发组不能为空")
                conds: list[Condition] = []
                for cond in group:
                    ref = cond["question_id"].strip()
                    if ref not in seen:
                        raise QCSpecError(
                            f"题目 {qid} 的条件引用了不存在或并非更早的题: {ref}"
                        )
                    conds.append(Condition(seen[ref], bool(cond["answer"])))
                groups.append(tuple(conds))
            trigger = tuple(groups)
        seen[qid] = i
        compiled.append(Question(qid, trigger))
    if len(compiled) > MAX_QUESTIONS:
        raise QCSpecError(f"题目至多 {MAX_QUESTIONS} 道")
    return compiled


def enumerate_paths(questions: list[Question]) -> frozenset[tuple[int, int]]:
    """枚举全部合法作答路径，返回去重后的 ``(被问题目位掩码, 答是位掩码)`` 集合。

    对每题穷举“是/否”的完整赋值；若该题在路径中未被询问，其赋值被忽略。
    因此不同赋值可能得到同一条合法路径，去重不影响“总是/从未/部分”的判定。
    """
    n = len(questions)
    paths: set[tuple[int, int]] = set()
    for assignment in range(1 << n):
        asked = 0
        yes = 0
        for i, question in enumerate(questions):
            if question.trigger is None:
                is_asked = True
            else:
                is_asked = False
                for group in question.trigger:
                    satisfied = True
                    for cond in group:
                        # 前题必须确实被问到，且答案等于期望值；
                        # 被跳过的题没有答案，不能触发后题。
                        if not ((asked >> cond.index) & 1):
                            satisfied = False
                            break
                        if bool((yes >> cond.index) & 1) != cond.expected:
                            satisfied = False
                            break
                    if satisfied:
                        is_asked = True
                        break
            if is_asked:
                asked |= 1 << i
                if (assignment >> i) & 1:
                    yes |= 1 << i
        paths.add((asked, yes))
    return frozenset(paths)


def _compatible(asked: int, yes: int, recorded: dict[int, bool]) -> bool:
    for index, answer in recorded.items():
        if not ((asked >> index) & 1):
            return False  # 已录答案出现在该路径应跳过的题上
        if bool((yes >> index) & 1) != answer:
            return False
    return True


def check_sheet(
    questions: list[Question],
    paths: frozenset[tuple[int, int]],
    answers: dict[str, bool],
) -> SheetResult:
    """核查单份部分答卷；不依赖其他答卷，因此单份矛盾不影响其余答卷。"""
    index_of = {q.question_id: i for i, q in enumerate(questions)}
    recorded: dict[int, bool] = {}
    for qid, answer in answers.items():
        key = qid.strip() if isinstance(qid, str) else qid
        if key not in index_of:
            raise QCSpecError(f"答卷含未知题目: {qid}")
        index = index_of[key]
        if index in recorded:
            raise QCSpecError(f"答卷中题目重复: {key}")
        recorded[index] = bool(answer)

    blanks = [i for i in range(len(questions)) if i not in recorded]
    compatible = [p for p in paths if _compatible(p[0], p[1], recorded)]

    if not compatible:
        return SheetResult(
            respondent_id="", status="contradiction",
            blank_count=len(blanks), findings=[],
        )

    findings: list[Finding] = []
    for i in blanks:
        appears = sum(1 for asked, _ in compatible if (asked >> i) & 1)
        if appears == len(compatible):
            classification = "missing"
        elif appears == 0:
            classification = "should_skip"
        else:
            classification = "needs_review"
        findings.append(Finding(questions[i].question_id, i + 1, classification))

    return SheetResult(
        respondent_id="", status="ok",
        blank_count=len(blanks), findings=findings,
    )


def check_questionnaire(
    raw_questions: list[dict], raw_sheets: list[dict]
) -> list[SheetResult]:
    """核查一批答卷。每份答卷独立判定，互不影响。"""
    questions = compile_questions(raw_questions)
    paths = enumerate_paths(questions)
    results: list[SheetResult] = []
    for raw in raw_sheets:
        result = check_sheet(questions, paths, raw["answers"])
        result.respondent_id = raw["respondent_id"]
        results.append(result)
    return results
