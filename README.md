# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 评审批次状态链

采购项目 → 投标 → **评审批次** → 评分（带重试编号）→ 授标，整条链可按批次从崩溃中恢复：

- **开标定批**：`POST /api/tenders/open` 时创建 `evaluation_batches` 批次，把本批参与投标（`bid_snapshot`）、评分项（`criteria_snapshot`）和项目版本（`tender_version`）固定下来。批次成员事后不改。
- **评分重试编号**：评分写入 `evaluations`，带 `batch_id` 与 `attempt_no`。完整有效评分拒绝覆盖；同一评审人对同一投标的半批残留重试时 `attempt_no+1`，旧行标 `superseded`。聚合与授标只读当前批 `score_status='valid'` 的行，因此恢复重试只保留同一批结果。
- **选择性失效**：开标后新增利益冲突，只把该评审人对相关供应商的有效评分置 `invalidated`；废标只失效该投标的评分。其余评分继续使用，批次按缺口自动回到 `active`。
- **投诉受理**：投诉写入后立即冻结授标（`tenders.award_frozen`）；投诉被**接受**时旧批次整体 `superseded` 并另建新批（轮次 +1），旧分不参与新批；投诉被驳回且无其他在办投诉、投诉批已评完时解冻。
- **评分不全禁授标**：`POST /api/tenders/award` 要求当前有效批次对全部仍有效投标评齐全部评分项，否则 409。
- **重启恢复**：服务启动时 `recover()` 只凭已写入记录判定每个活动批次：
  - `continue`：有缺口但无崩溃/失效痕迹，等评审人继续补分；
  - `reevaluate`：缺口由崩溃半批残留或被失效评分造成，需要重评/重试；
  - `complete`：全部有效投标评分齐全，可以授标；
  - `finished`：批次已被替代或项目已授标/取消。
  - 早期无批次的数据库会自动迁移：旧 `evaluations` 重命名为 `evaluations_legacy` 并回填到第 1 批。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `GET /api/tenders/{id}/batch`：当前活动批次的判定结论、成员/评分项快照、评分缺口、批次历史
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标、核验承诺哈希并创建第 1 批评审批次
- `POST /api/conflicts`、`POST /api/evaluations`（返回 `batch_id`、`attempt_no` 与批次进度）
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`（受理即冻结授标）、`POST /api/complaints/resolve`（接受即另开批次）
- `POST /api/tenders/award`：校验当前批次评分齐全后锁定并保存含 `batch_id` 的排名快照

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分拒绝、批次快照、崩溃半批重试只保留同批结果、冲突/废标选择性失效、投诉另开批次旧分隔离、评分不全禁授标、重启 continue/reevaluate/finished 判定和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。冲突一经申报即不可撤回，失效评分需要无冲突评审人重新提交。
