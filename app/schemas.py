"""请求/响应模型与严格输入校验。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .qc import MAX_QUESTIONS

MAX_SAMPLES = 200
MAX_TARGETS = 12
MAX_D = 100
MAX_CODE_LEN = 64


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _strict_int(name: str, minimum: int, maximum: int | None = None):
    # mode="before"：在 Pydantic 类型强转之前拦截，
    # 否则 True 会先被强转为 1，布尔冒充整数将无法识别。
    def _v(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须是整数")
        if value < minimum:
            raise ValueError(f"{name} 必须 >= {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{name} 必须 <= {maximum}")
        return value
    return field_validator(name, mode="before")(_v)


def _strict_code(name: str):
    def _v(value):
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError(f"{name} 必须是非空字符串")
        value = value.strip()
        if not value:
            raise ValueError(f"{name} 不能为空字符串")
        if len(value) > MAX_CODE_LEN:
            raise ValueError(f"{name} 长度须 <= {MAX_CODE_LEN}")
        return value
    return field_validator(name, mode="before")(_v)


class SampleIn(StrictModel):
    person_id: str = Field(description="样本人员编号，非空，单次提交内唯一")
    region: str = Field(description="地区代码")
    age_group: str = Field(description="年龄段代码")
    d: int = Field(description="正整数基础权重，1 <= d <= 100")

    _v_id = _strict_code("person_id")
    _v_region = _strict_code("region")
    _v_age = _strict_code("age_group")
    _v_d = _strict_int("d", 1, MAX_D)


class SamplesPayload(StrictModel):
    samples: list[SampleIn] = Field(description=f"至多 {MAX_SAMPLES} 人")

    @field_validator("samples")
    @classmethod
    def _check(cls, v: list[SampleIn]):
        if len(v) > MAX_SAMPLES:
            raise ValueError(f"样本数至多 {MAX_SAMPLES} 人")
        ids = [s.person_id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("person_id 在本次提交中存在重复")
        return v


class TargetIn(StrictModel):
    dimension: Literal["region", "age_group"] = Field(
        description="目标维度：region 或 age_group"
    )
    code: str = Field(description="维度取值代码（对应样本的 region/age_group）")
    population: int = Field(description="非负整数人口总量")

    _v_code = _strict_code("code")
    _v_pop = _strict_int("population", 0)


class TargetsPayload(StrictModel):
    targets: list[TargetIn] = Field(description=f"至多 {MAX_TARGETS} 项")

    @field_validator("targets")
    @classmethod
    def _check(cls, v: list[TargetIn]):
        if len(v) > MAX_TARGETS:
            raise ValueError(f"目标至多 {MAX_TARGETS} 项")
        keys = [(t.dimension, t.code) for t in v]
        if len(set(keys)) != len(keys):
            raise ValueError("(dimension, code) 在本次提交中存在重复")
        return v


class WeightItem(BaseModel):
    person_id: str
    d: int
    w: float
    lower: float
    upper: float


# ---------------------------------------------------------------- 问卷质控
def _strict_bool(name: str):
    def _v(value):
        if not isinstance(value, bool):
            raise ValueError(f"{name} 必须是布尔值 true/false")
        return value
    return field_validator(name, mode="before")(_v)


class TriggerConditionIn(StrictModel):
    question_id: str = Field(description="所引用的前题编号（必须出现在更早的位置）")
    answer: bool = Field(description="前题须给出的答案：true=是，false=否")

    _v_qid = _strict_code("question_id")
    _v_answer = _strict_bool("answer")


class QuestionIn(StrictModel):
    question_id: str = Field(description="题目编号，整份问卷内唯一")
    trigger: list[list[TriggerConditionIn]] | None = Field(
        default=None,
        description=(
            "null 表示无条件询问；否则为触发组列表。任一组条件全部满足即询问，"
            "组间“或”、组内“与”；被跳过的前题没有答案，不能触发后题。"
        ),
    )

    _v_qid = _strict_code("question_id")

    @field_validator("trigger")
    @classmethod
    def _check_trigger(cls, v):
        if v is None:
            return v
        if not v:
            raise ValueError("trigger 须为 null 或至少包含一组条件")
        for group in v:
            if len(group) == 0:
                raise ValueError("触发组不能为空")
        return v


class SheetIn(StrictModel):
    respondent_id: str = Field(description="受访者编号，须在该版样本中存在且不重复")
    answers: dict[str, bool] = Field(
        default_factory=dict,
        description="已录答案：question_id -> true/false；未出现的题视为空白",
    )

    _v_rid = _strict_code("respondent_id")

    @field_validator("answers", mode="before")
    @classmethod
    def _check_answers(cls, v):
        # mode="before"：Pydantic 会把 1/0 强转成 bool，必须在强转前拦截，
        # 否则整数/字符串会冒充“是/否”。
        if not isinstance(v, dict):
            raise ValueError("answers 必须是 question_id -> true/false 的对象")
        out: dict[str, bool] = {}
        for qid, answer in v.items():
            if isinstance(qid, bool) or not isinstance(qid, str) or not qid.strip():
                raise ValueError("answers 的题号必须是非空字符串")
            key = qid.strip()
            if key in out:
                raise ValueError(f"answers 中的题号重复: {key}")
            if not isinstance(answer, bool):
                raise ValueError(f"题目 {key} 的答案必须是布尔值 true/false")
            out[key] = answer
        return out


class QuestionnaireQCPayload(StrictModel):
    version_id: int = Field(description="调用方认定的输入版本号（epoch），须为当前版本")
    questions: list[QuestionIn] = Field(
        description=f"按询问顺序排列的是非题，1–{MAX_QUESTIONS} 道"
    )
    sheets: list[SheetIn] = Field(description="部分答卷，按受访者编号提交")

    _v_version = _strict_int("version_id", 1)

    @field_validator("questions")
    @classmethod
    def _check_questions(cls, v: list[QuestionIn]):
        if len(v) > MAX_QUESTIONS:
            raise ValueError(f"题目至多 {MAX_QUESTIONS} 道")
        ids = [q.question_id for q in v]
        if len(set(ids)) != len(ids):
            raise ValueError("question_id 在整份问卷中不得重复")
        earlier: set[str] = set()
        for q in v:
            for group in (q.trigger or []):
                for cond in group:
                    if cond.question_id not in earlier:
                        raise ValueError(
                            f"题目 {q.question_id} 的条件引用了不存在或并非更早的题: "
                            f"{cond.question_id}"
                        )
            earlier.add(q.question_id)
        return v

    @field_validator("sheets")
    @classmethod
    def _check_sheets(cls, v: list[SheetIn]):
        ids = [s.respondent_id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("respondent_id 在本次提交中存在重复")
        return v


class FindingOut(BaseModel):
    question_id: str
    position: int
    classification: str
    label: str


class SheetQCResult(BaseModel):
    respondent_id: str
    status: str
    status_label: str
    blank_count: int
    findings: list[FindingOut]


class QCSummary(BaseModel):
    sheets: int
    ok: int
    contradiction: int
    missing: int
    should_skip: int
    needs_review: int


class QCResponse(BaseModel):
    version_id: int
    summary: QCSummary
    sheets: list[SheetQCResult]


class TargetTotalsItem(BaseModel):
    dimension: str
    code: str
    population: int
    weighted_total: float
    abs_error: float


class JobResult(BaseModel):
    job_id: str
    epoch: int
    status: str
    submitted_at: str
    finished_at: str | None = None
    input_snapshot: dict
    error: str | None = None
    # status=succeeded 时完整给出，其余状态为 None，绝不开放半份结果
    weights: list[WeightItem] | None = None
    objective: float | None = None
    residual_abs_sum: float | None = None
    residual_max_abs: float | None = None
    target_totals: list[TargetTotalsItem] | None = None
    # status=infeasible 时给出最小冲突目标子集
    infeasible_targets: list[dict] | None = None
