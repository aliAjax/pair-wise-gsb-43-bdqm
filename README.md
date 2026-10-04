# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、批次化规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。旧库（只有 `evaluation_round`）启动时自动迁移为批次链并保留旧评分。

## 评审状态链

采购项目 → 开标/投诉产生**评分批次** → 批次固定名册 → 带重试编号的评分 → 批次完成 → 授标。

- `evaluation_batches`：每批固定评分项快照（`criteria`）、产生时项目版本（`origin_tender_version`）、状态 `open/completed/awarded/cancelled`。
- `batch_bids`：批次固定的参与投标及其版本；废标后置 `required=0` 保留历史，不影响其余投标结果。
- `evaluations`：归属 `batch_id`，带 `attempt_no` 重试编号与 `valid/invalid_reason`。

关键规则：

1. **批次固定**：开标即建首批，名册只含当时已开标的有效投标；之后新投标进不了旧批。
2. **崩溃重试只留同一批结果**：同一 `attempt_no` 可在崩溃后部分补齐，已写入的评分项不重复；完整后同号拒绝覆盖。重评须用连续的新 `attempt_no`，旧 attempt 由该评审人对该投标的全部评分置 `superseded`。
3. **失效不波及他人**：利益冲突变化只置该评审人相关评分为 `conflict` 失效；废标只置该投标评分为 `bid_inactive`，其余结果继续使用。
4. **投诉另建批次**：受理即 `award_frozen=1` 冻结授标与评分，并建未激活的 `complaint` 批次（空名册结果）；受理成立后激活新批、旧批分数不迁移，驳回则 `cancelled` 新批、指针回到旧批继续。
5. **评分不全不得授标**：授标事务内重算批次，缺任一有效评分单元即拒绝；`UPDATE ... WHERE version=? AND status IN(...)` 守卫并发授标。
6. **重启重放**：服务启动 `recover_all()` 仅凭已写入记录判定当前批次 `continue`（继续）/ `reassess`（有失效需重评）/ `ready`（可授标）/ `frozen`（投诉冻结）/ `ended`（已授标）。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标、核验承诺哈希并建立首批
- `POST /api/tenders/recover`：重放项目批次链，返回各批及当前批继续/重评/结束判定
- `POST /api/conflicts`、`POST /api/conflicts/retract`
- `POST /api/evaluations`：`bid_id`、`values`、`attempt_no`（默认 1，崩溃补齐用同号，重评递增）
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`（受理即冻结+建批）、`POST /api/complaints/resolve`（`accepted` 激活新批 / `rejected` 撤销新批）
- `POST /api/tenders/award`：重算批次、校验评分齐全后锁定并保存排名快照

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_flow.py`：完整开标授标、截止前正文隐藏、利益冲突、重复评分、投诉重评、角色权限。
- `tests/test_batches.py`：批次固定名册、崩溃同号重试、新 attempt 作废旧分、冲突/废标局部失效、投诉冻结与新旧批隔离、驳回回旧批、重启重放、并发授标。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
