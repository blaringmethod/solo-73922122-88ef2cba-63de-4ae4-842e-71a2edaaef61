"""FastAPI 应用：样本/目标更正、后台权重计算、结果取用。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .db import Database
from .jobs import JobManager
from .qc import CLASSIFICATION_LABELS, STATUS_LABELS, check_questionnaire
from .schemas import (
    JobResult,
    QuestionnaireQCPayload,
    QCResponse,
    SamplesPayload,
    TargetsPayload,
    TargetTotalsItem,
    WeightItem,
)

DB_PATH = os.environ.get("WEIGHTS_DB", "/data/weights.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    app.state.db = Database(DB_PATH)
    app.state.jobs = JobManager(app.state.db)
    yield
    app.state.jobs.shutdown()
    app.state.db.close()


app = FastAPI(
    title="回访问卷样本权重校正 API",
    version="1.0.0",
    description=(
        "根据地区/年龄段人口总量，对至多 200 人的基础权重 d 做二次规划校正，"
        "满足 0.5d ≤ w ≤ 2d 且目标人群权重合计等于人口总量，"
        "最小化 Σ(w−d)²/d。任何样本/目标修改都会令旧结果与在途计算永久失效。"
    ),
    lifespan=lifespan,
)


def db() -> Database:
    return app.state.db


def jobs() -> JobManager:
    return app.state.jobs


# ---------------------------------------------------------------- 输入更正
@app.put("/samples", status_code=200, tags=["输入数据"],
         summary="整体替换样本（至多 200 人；使所有旧结果与在途计算失效）")
def put_samples(payload: SamplesPayload):
    samples = [s.model_dump() for s in payload.samples]
    epoch = db().replace_samples(samples)
    return {"epoch": epoch, "count": len(samples),
            "message": "样本已更新，旧结果与在途计算已永久失效，须重新计算"}


@app.get("/samples", tags=["输入数据"], summary="查看当前样本")
def get_samples():
    snap = db().snapshot()
    return {"epoch": snap["epoch"], "samples": snap["samples"]}


@app.put("/targets", status_code=200, tags=["输入数据"],
         summary="整体替换人口目标（至多 12 项；使所有旧结果与在途计算失效）")
def put_targets(payload: TargetsPayload):
    targets = [t.model_dump() for t in payload.targets]
    epoch = db().replace_targets(targets)
    return {"epoch": epoch, "count": len(targets),
            "message": "目标已更新，旧结果与在途计算已永久失效，须重新计算"}


@app.get("/targets", tags=["输入数据"], summary="查看当前人口目标")
def get_targets():
    snap = db().snapshot()
    return {"epoch": snap["epoch"], "targets": snap["targets"]}


# ---------------------------------------------------------------- 计算/结果
@app.post("/calculate", status_code=202, tags=["计算"],
          summary="提交后台权重计算（不阻塞，返回 job_id）")
def calculate():
    snap = db().snapshot()
    if not snap["samples"]:
        raise HTTPException(422, "当前没有样本，无法计算：请先 PUT /samples")
    if not snap["targets"]:
        raise HTTPException(422, "当前没有目标，无法计算：请先 PUT /targets")
    job = jobs().submit()
    return JSONResponse(
        status_code=202,
        content={"job_id": job["job_id"], "epoch": job["epoch"],
                 "status": job["status"],
                 "poll": f"/jobs/{job['job_id']}"},
    )


def _serialize_job(row: dict) -> dict:
    result = json.loads(row["result"]) if row["result"] else None
    weights = totals = None
    objective = l1 = lmax = None
    iis = None
    if row["status"] == "succeeded" and result:
        weights = [WeightItem(**x).model_dump() for x in result["weights"]]
        totals = [TargetTotalsItem(**x).model_dump() for x in result["target_totals"]]
        objective = result["objective"]
        l1 = result["residual_abs_sum"]
        lmax = result["residual_max_abs"]
    elif row["status"] == "infeasible" and result:
        iis = result["infeasible_targets"]

    out = JobResult(
        job_id=row["job_id"],
        epoch=row["epoch"],
        status=row["status"],
        submitted_at=row["submitted_at"],
        finished_at=row["finished_at"],
        input_snapshot=json.loads(row["input_snapshot"]),
        error=row["error"],
        weights=weights,
        objective=objective,
        residual_abs_sum=l1,
        residual_max_abs=lmax,
        target_totals=totals,
        infeasible_targets=iis,
    )
    return out.model_dump(exclude_none=False)


@app.get("/jobs", tags=["计算"], summary="列出最近作业")
def list_jobs():
    return {"epoch": db().get_epoch(), "jobs": db().list_jobs()}


@app.get("/jobs/{job_id}", tags=["计算"],
         summary="查询作业状态与完整结果（仅成功时返回权重，失败/中断无半份结果）")
def get_job(job_id: str):
    row = db().get_job(job_id)
    if row is None:
        raise HTTPException(404, "作业不存在或已因输入变更而永久失效")
    return _serialize_job(row)


@app.get("/result", tags=["计算"],
         summary="取用与当前输入一致的完整权重；无可用完整结果时返回 409")
def get_result():
    row = db().latest_succeeded_job()
    if row is None:
        raise HTTPException(
            409,
            "当前没有与现有输入一致的完整权重结果（尚未计算、计算中、冲突、"
            "数值失败或进程重启均不开放半份结果）",
        )
    data = _serialize_job(dict(row))
    # 一致性核对：作业快照必须与当前输入逐字段一致
    current = db().snapshot()
    snap = data["input_snapshot"]
    if snap.get("samples") != current["samples"] or \
            snap.get("targets") != current["targets"] or \
            snap.get("epoch") != current["epoch"]:
        # 理论上不会发生（epoch 变更即清除作业），双保险
        raise HTTPException(409, "结果与当前输入不一致，已拒绝取用，请重新计算")
    return data


# ---------------------------------------------------------------- 问卷质控
@app.post("/qc/questionnaire", tags=["问卷质控"], response_model=QCResponse,
          summary="核查部分答卷：标记漏答 / 应跳 / 待复核 / 矛盾",
          responses={
              409: {"description": "版本号过期，或核查期间样本/目标被更正（整批作废）"},
              422: {"description": "问卷定义、答卷、受访者编号或重复提交不合法"},
          })
def qc_questionnaire(payload: QuestionnaireQCPayload):
    """只核查空白题，不改动样本/目标，也不写入任何版本或作业。

    核查期间样本或目标被更正（epoch 变化）时，整批返回 409，请以最新版本重核。
    受访者编号必须在该版样本中且单批内不得重复；版本号过期同样 409。
    """
    snapshot = db().snapshot()
    if payload.version_id != snapshot["epoch"]:
        return JSONResponse(status_code=409, content={
            "detail": (
                f"版本号已过期：提交 version_id={payload.version_id}，"
                f"当前 epoch={snapshot['epoch']}。请以最新版本重新提交核查。"
            ),
            "version_id": payload.version_id,
            "current_version_id": snapshot["epoch"],
        })

    known_respondents = {
        s["person_id"] for s in snapshot["samples"]
    }
    for sheet in payload.sheets:
        if sheet.respondent_id not in known_respondents:
            raise HTTPException(
                422,
                f"受访者 {sheet.respondent_id} 不在该版样本中（version_id="
                f"{payload.version_id}）",
            )

    try:
        results = check_questionnaire(
            [q.model_dump() for q in payload.questions],
            [s.model_dump() for s in payload.sheets],
        )
    except ValueError as exc:  # 防御性：Pydantic 已拦截绝大多数非法定义
        raise HTTPException(422, str(exc))

    # 核查是纯计算；若期间输入被更正，整批结果作废。
    if db().get_epoch() != snapshot["epoch"]:
        return JSONResponse(status_code=409, content={
            "detail": (
                "核查期间样本或人口目标被更正，整批结果已作废。"
                "请以最新版本重新提交核查。"
            ),
            "version_id": payload.version_id,
            "current_version_id": db().get_epoch(),
        })

    sheets_out = []
    counts = {"missing": 0, "should_skip": 0, "needs_review": 0}
    contradictions = 0
    for result in results:
        if result.status == "contradiction":
            contradictions += 1
        findings = []
        for finding in result.findings:
            counts[finding.classification] += 1
            findings.append({
                "question_id": finding.question_id,
                "position": finding.position,
                "classification": finding.classification,
                "label": CLASSIFICATION_LABELS[finding.classification],
            })
        sheets_out.append({
            "respondent_id": result.respondent_id,
            "status": result.status,
            "status_label": STATUS_LABELS[result.status],
            "blank_count": result.blank_count,
            "findings": findings,
        })

    return QCResponse(
        version_id=snapshot["epoch"],
        summary={
            "sheets": len(results),
            "ok": len(results) - contradictions,
            "contradiction": contradictions,
            **counts,
        },
        sheets=sheets_out,
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {"status": "ok", "epoch": db().get_epoch()}
