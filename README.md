# 回访问卷样本权重校正 API

调查数据工程师用于校正回访问卷样本偏差的服务：为每位受访者求一个校正权重
`w`，使各目标人群（按地区或年龄段）的权重合计等于给定人口总量，同时保持
个人权重界限 `0.5d ≤ w ≤ 2d`，并最小化与基础权重 `d` 的加权二次距离
`Σ (w−d)² / d`。服务另提供问卷质控接口 `POST /qc/questionnaire`，
按版本号核查部分答卷的空白题（漏答 / 应跳 / 待复核 / 矛盾），见下文。

## 数学模型

给定 n 名样本（n ≤ 200），基础权重 `d_i ∈ {1,…,100}`，以及 m 项目标
（m ≤ 12，维度为 `region` 或 `age_group`）：

```
min   Σ_i (w_i − d_i)² / d_i
s.t.  A w = p                # 每行一个目标人群，A 为 0/1 成员矩阵（允许线性冗余）
      0.5 d_i ≤ w_i ≤ 2 d_i
```

- **可行性与求解分离**：先用 SciPy `linprog`（HiGHS）解松弛 LP 独立判定可行性；
  可行后再用 SLSQP 解凸 QP，残差不达标时以活动集 KKT 精修、`trust-constr` 兜底。
  **数值求解失败返回 `failed`，绝不被当作无解。**
- **无解诊断**：不可行时返回一个最小不可行目标子集（IIS，贪心删除法），
  删去其中任意一项目标后其余目标即在个人界限下恢复可行；个人权重界限始终保留。
- **精度**：每个目标人群的合计误差 `|Σw − population| ≤ 1e-6`（响应中逐项目回报）。
- **冗余目标**：约束矩阵行线性相关（如“全员合计 = 各地区之和”）时正常求解。

## 失效与一致性语义

- 计算在后台线程池执行，HTTP 请求仅入队，**不阻塞样本/目标更正**。
- 任何一次已提交的样本或目标修改（即使内容与原来完全相同、即使之后又改回去）
  都会使全局版本号 `epoch` 加一：所有历史作业被删除、在途计算通过取消事件
  永久放弃且不写结果。旧结果不会因“改回原值”而恢复，必须重新计算。
- 只有作业成功结束时才原子写入**完整**权重结果；`queued/running/failed/
  infeasible/interrupted` 状态均不返回任何半份权重。
- 进程重启后，未完成作业标记为 `interrupted`；`GET /result` 仅提供与当前
  输入逐字段一致的完整结果，否则返回 `409`。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# API 与交互式文档：
#   http://localhost:8000/docs      （Swagger UI，可直接试调）
#   http://localhost:8000/redoc
#   http://localhost:8000/openapi.json
```

SQLite 数据库保存在命名卷 `weights-data`（容器内 `/data/weights.db`）。

## 本地无 Docker 运行（可选）

```bash
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/samples` | 整体替换样本（≤200 人；使旧结果与在途计算失效） |
| GET | `/samples` | 查看当前样本与 epoch |
| PUT | `/targets` | 整体替换人口目标（≤12 项；同上失效） |
| GET | `/targets` | 查看当前目标 |
| POST | `/calculate` | 提交后台计算，返回 `job_id`（202） |
| GET | `/jobs/{job_id}` | 查询作业；成功时含完整权重与输入快照 |
| GET | `/jobs` | 最近作业列表 |
| GET | `/result` | 取用与当前输入一致的完整权重，否则 409 |
| POST | `/qc/questionnaire` | 核查部分答卷的空白题：漏答 / 应跳 / 待复核 / 矛盾 |
| GET | `/health` | 健康检查 |

输入为严格整数：`d` 必须是 1–100 的整数（布尔值、小数、越界均 422），
`population` 为非负整数；`person_id` 与 `(dimension, code)` 单次提交内不得重复。

## 问卷质控：空白题核查

`POST /qc/questionnaire` 供质控员在交分析员前找出“本该回答却空着”的题。
它只读取当前样本的受访者编号，**不修改**样本/目标，也不产生作业或版本。

请求：

```json
{
  "version_id": 7,
  "questions": [
    {"question_id": "q1"},
    {"question_id": "q2"},
    {"question_id": "q3", "trigger": [[{"question_id": "q1", "answer": true}]]},
    {"question_id": "q4", "trigger": [
      [{"question_id": "q2", "answer": true}],
      [{"question_id": "q3", "answer": false}]
    ]}
  ],
  "sheets": [
    {"respondent_id": "R1", "answers": {"q1": false, "q2": true}},
    {"respondent_id": "R2", "answers": {}}
  ]
}
```

- 题目 1–12 道，按询问顺序排列；`trigger: null`（或缺省）表示无条件询问。
- `trigger` 是触发组列表：**任一组**条件全部满足即询问，组内“与”、组间“或”。
- 条件只能引用更早的题；被跳过的题没有答案，**不能触发后题**。
- `sheets` 可只含部分题目的答案；未出现的题即空白。
- `version_id` 必须是当前 `epoch`；受访者编号须在该版样本中，且单批内不得重复
  （否则 422，整批拒绝）。

判定语义（在“与已录答案相容的合法作答路径”上考察）：

| 结果 | 含义 |
| --- | --- |
| `missing` / 漏答 | 空白题在**所有**相容路径中都被问到 |
| `should_skip` / 应跳 | 空白题在**任何**相容路径中都未被问到 |
| `needs_review` / 待复核 | 只在部分相容路径中被问到 |
| `contradiction` / 矛盾 | 不存在相容路径（如跳过题上录了答案，或答案与触发条件冲突）；该答卷不做逐题分类，其余答卷照常核查 |

`version_id` 过期，或核查期间样本/人口目标被更正，**整批返回 409**，
响应含 `current_version_id`，请以最新版本重核。响应同时给出逐题结果
（含顺位 `position` 与中文 `label`）和批次汇总。

示例数据：`examples/qc_questions.json`、`examples/qc_sheets.json`。

## 示例与演示

`examples/` 提供可行、冲突两套权重数据，以及问卷质控的题目与答卷：

```bash
# 先启动服务，然后：
python3 examples/demo.py                      # 默认 http://127.0.0.1:8000
python3 examples/demo.py http://host:8000
```

手动调用：

```bash
curl -X PUT localhost:8000/samples -H 'Content-Type: application/json' \
  --data @examples/feasible_samples.json
curl -X PUT localhost:8000/targets -H 'Content-Type: application/json' \
  --data @examples/feasible_targets.json
curl -X POST localhost:8000/calculate
curl localhost:8000/jobs/<job_id>
curl localhost:8000/result
```

冲突示例（`conflict_*.json`）：地区 R1 要求合计 4，但 R1 两人权重上限之和
仅为 4，与年龄段 a1 要求合计 1 无法同时满足，返回两项目标组成的最小冲突集。

## 测试

```bash
pip install pytest
pytest -q
```

覆盖：随机可行案例的 KKT 最优性、200×12 满规模、冗余等式、零/未覆盖目标、
IIS 最小性、边界解、严格校验、失效语义、在途修改取消、重启中断、
数值失败与无解的区分；以及问卷质控的跳题路径枚举、三类空白判定、
矛盾答卷与其余答卷隔离、版本/样本成员/重复校验、核查期间 409。
