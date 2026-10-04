"""Sealed public-procurement tendering and evaluation service.

评审状态链
==========

采购项目 (tenders) -> 投标 (bids) -> 评审批次 (evaluation_batches) -> 评分 (evaluations) -> 授标

- 开标时创建一个评审批次，把本批参与投标、评分项、项目版本快照固定下来；
  之后废标、冲突、投诉都不会改写批次成员，只会让相关评分失效或让批次被替代。
- 每条评分带 attempt_no：同一评审人对同一投标同一评分项的一次重试会新建一行，
  旧的半批/失败行标记为 superseded，聚合时只取有效行。
- 冲突变化或投标被废标后，只把相关评分置为 invalidated，其余结果继续可用，
  批次按缺口回到“继续/重评”。
- 投诉受理后先冻结授标，投诉被接受时旧批次整体 superseded 并另建新批，
  旧分不进入新批；投诉被驳回且无其他在办投诉时解冻。
- 服务重启后只依据已写入记录恢复：孤儿半批尝试行被判定为重评，批次判定为
  continue / reevaluate / complete / finished。
- 只有当前有效批次完成全部有效投标的全部评分项，且授标未冻结时才能授标。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "public_procurement.db"

# 评分行有效性状态：有效 / 被冲突或废标失效 / 被更新重试替代
SCORE_VALID = "valid"
SCORE_INVALIDATED = "invalidated"
SCORE_SUPERSEDED = "superseded"

# 批次判定结论：继续本批 / 本批需重评 / 本批完成可授标 / 项目已结束
BATCH_CONTINUE = "continue"
BATCH_REEVALUATE = "reevaluate"
BATCH_COMPLETE = "complete"
BATCH_FINISHED = "finished"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("时间格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def columns_exist(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()
        self.recover()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    deadline TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '[]',
                    evaluation_round INTEGER NOT NULL DEFAULT 0,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
                    award_frozen INTEGER NOT NULL DEFAULT 0,
                    active_batch_id INTEGER,
                    awarded_bid_id INTEGER,
                    award_snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    representative TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sealed',
                    version INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    opened_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS evaluation_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    batch_no INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    reason TEXT NOT NULL DEFAULT 'open',
                    bid_snapshot TEXT NOT NULL DEFAULT '[]',
                    criteria_snapshot TEXT NOT NULL DEFAULT '[]',
                    tender_version INTEGER NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    superseded_at TEXT,
                    UNIQUE(tender_id,batch_no)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES evaluation_batches(id),
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_round INTEGER NOT NULL,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    evaluator TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    score REAL NOT NULL,
                    score_status TEXT NOT NULL DEFAULT 'valid',
                    invalidate_reason TEXT NOT NULL DEFAULT '',
                    comment TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    declared_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,evaluator,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER REFERENCES vendors(id),
                    question TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    answered_by TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    complainant TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id,status);
                CREATE INDEX IF NOT EXISTS idx_batches_tender ON evaluation_batches(tender_id,status);
                """
            )
            self._migrate(conn)
            # evaluations 可能来自旧库重建，索引在迁移之后再建（均为 IF NOT EXISTS，幂等）
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_eval_batch ON evaluations(batch_id,score_status);
                CREATE INDEX IF NOT EXISTS idx_eval_bid_batch ON evaluations(bid_id,batch_id,score_status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_unique
                    ON evaluations(batch_id,bid_id,evaluator,criterion,attempt_no);
                """
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把早期无批次版本的库补齐列，并把旧评分整体搬进第 1 批。"""
        tender_cols = columns_exist(conn, "tenders")
        if "award_frozen" not in tender_cols:
            conn.execute("ALTER TABLE tenders ADD COLUMN award_frozen INTEGER NOT NULL DEFAULT 0")
        if "active_batch_id" not in tender_cols:
            conn.execute("ALTER TABLE tenders ADD COLUMN active_batch_id INTEGER")
        if "evaluation_round" not in tender_cols:
            conn.execute("ALTER TABLE tenders ADD COLUMN evaluation_round INTEGER NOT NULL DEFAULT 0")

        eval_cols = columns_exist(conn, "evaluations")
        if not eval_cols:
            return
        if {"batch_id", "attempt_no", "score_status"} <= eval_cols:
            return

        conn.execute("ALTER TABLE evaluations RENAME TO evaluations_legacy")
        conn.executescript(
            """
            CREATE TABLE evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES evaluation_batches(id),
                bid_id INTEGER NOT NULL REFERENCES bids(id),
                evaluation_round INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                evaluator TEXT NOT NULL,
                criterion TEXT NOT NULL,
                raw_value REAL NOT NULL,
                score REAL NOT NULL,
                score_status TEXT NOT NULL DEFAULT 'valid',
                invalidate_reason TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_eval_batch ON evaluations(batch_id,score_status);
            CREATE INDEX idx_eval_bid_batch ON evaluations(bid_id,batch_id,score_status);
            CREATE UNIQUE INDEX idx_eval_unique
                ON evaluations(batch_id,bid_id,evaluator,criterion,attempt_no);
            """
        )
        legacy = conn.execute("SELECT * FROM evaluations_legacy").fetchall()
        if not legacy:
            return
        now = utcnow()
        for tender_id, in conn.execute("SELECT DISTINCT tender_id FROM bids").fetchall():
            tender = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
            if tender is None:
                continue
            rows = conn.execute(
                """SELECT b.* FROM bids b JOIN evaluations_legacy e ON e.bid_id=b.id
                   WHERE b.tender_id=? GROUP BY b.id""",
                (tender_id,),
            ).fetchall()
            snapshot = [
                {"bid_id": r["id"], "vendor_id": r["vendor_id"], "price": r["price"],
                 "status": "disqualified" if r["status"] == "disqualified" else "eligible"}
                for r in rows
            ]
            cur = conn.execute(
                """INSERT INTO evaluation_batches
                       (tender_id,batch_no,status,reason,bid_snapshot,criteria_snapshot,tender_version,
                        created_by,created_at,completed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tender_id, 1, "active", "migrated",
                 json.dumps(snapshot, ensure_ascii=False), tender["criteria"], tender["version"],
                 "system-migration", now, None),
            )
            batch_id = cur.lastrowid
            conn.execute(
                "UPDATE tenders SET evaluation_round=1,active_batch_id=? WHERE id=?",
                (batch_id, tender_id),
            )
            conn.execute(
                """INSERT INTO evaluations
                       (batch_id,bid_id,evaluation_round,attempt_no,evaluator,criterion,raw_value,score,
                        score_status,comment,version,created_at,updated_at)
                   SELECT ?,bid_id,1,1,evaluator,criterion,raw_value,score,'valid',comment,1,created_at,updated_at
                   FROM evaluations_legacy WHERE bid_id IN (SELECT id FROM bids WHERE tender_id=?)""",
                (batch_id, tender_id),
            )

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(tender_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (tender_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise DomainError("采购项目不存在", 404)
        return row

    def _active_batch(self, conn: sqlite3.Connection, tender: sqlite3.Row | dict[str, Any]) -> sqlite3.Row | None:
        batch_id = tender["active_batch_id"]
        if batch_id is None:
            return None
        return conn.execute("SELECT * FROM evaluation_batches WHERE id=?", (batch_id,)).fetchone()

    # ------------------------------------------------------------------
    # 批次状态链
    # ------------------------------------------------------------------
    def _open_batch(self, conn: sqlite3.Connection, tender: sqlite3.Row,
                    bids: list[sqlite3.Row], reason: str, actor: str, now: str) -> sqlite3.Row:
        """固定本批参与投标、评分项和项目版本，写入新的评审批次。"""
        next_no = conn.execute(
            "SELECT COALESCE(MAX(batch_no),0)+1 AS n FROM evaluation_batches WHERE tender_id=?",
            (tender["id"],),
        ).fetchone()["n"]
        snapshot = [
            {"bid_id": bid["id"], "vendor_id": bid["vendor_id"], "price": bid["price"], "status": "eligible"}
            for bid in bids
        ]
        cur = conn.execute(
            """INSERT INTO evaluation_batches
                   (tender_id,batch_no,status,reason,bid_snapshot,criteria_snapshot,tender_version,
                    created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (tender["id"], next_no, "active", reason,
             json.dumps(snapshot, ensure_ascii=False), tender["criteria"], tender["version"],
             actor, now),
        )
        batch = conn.execute("SELECT * FROM evaluation_batches WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.execute(
            "UPDATE tenders SET active_batch_id=?,evaluation_round=? WHERE id=?",
            (batch["id"], next_no, tender["id"]),
        )
        self._audit(conn, tender["id"], actor, "batch.opened",
                    {"batch_id": batch["id"], "batch_no": next_no, "bid_count": len(snapshot), "reason": reason})
        return batch

    def _batch_progress(self, conn: sqlite3.Connection, batch: sqlite3.Row | dict[str, Any],
                        tender: sqlite3.Row | dict[str, Any] | None = None) -> dict[str, Any]:
        """只依据已写入记录计算批次缺口与判定结论（不写库）。"""
        members = [m for m in json.loads(batch["bid_snapshot"]) if m["status"] == "eligible"]
        # 废标是批次成员之外的运行时事实：被废标的投标立即退出有效集合
        if tender is None:
            tender = self._tender(conn, batch["tender_id"])
        live_bid_ids = {
            r["id"] for r in conn.execute(
                "SELECT id FROM bids WHERE tender_id=? AND status IN ('opened','qualified')",
                (batch["tender_id"],),
            ).fetchall()
        }
        effective = [m for m in members if m["bid_id"] in live_bid_ids]
        criteria = json.loads(batch["criteria_snapshot"])
        criterion_names = [c["name"] for c in criteria]

        scored_rows = conn.execute(
            """SELECT bid_id,criterion,COUNT(DISTINCT evaluator) AS evaluators
               FROM evaluations WHERE batch_id=? AND score_status=?
               GROUP BY bid_id,criterion""",
            (batch["id"], SCORE_VALID),
        ).fetchall()
        scored: dict[int, set[str]] = {}
        for row in scored_rows:
            scored.setdefault(row["bid_id"], set()).add(row["criterion"])

        invalidated = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE batch_id=? AND score_status=?",
            (batch["id"], SCORE_INVALIDATED),
        ).fetchone()["c"]
        # 崩溃残留：某次尝试写入过半批行后服务中断，恢复时已被标记 superseded。
        # 只要该投标仍有缺口，就说明这批需要“重评/重试”而不是普通“继续”。
        crashed_rows = conn.execute(
            """SELECT bid_id,COUNT(DISTINCT evaluator) AS crashed_evaluators FROM evaluations
               WHERE batch_id=? AND score_status=? AND invalidate_reason LIKE '崩溃残留%'
               GROUP BY bid_id""",
            (batch["id"], SCORE_SUPERSEDED),
        ).fetchall()
        crashed_bids = {r["bid_id"] for r in crashed_rows}

        missing: dict[str, list[str]] = {}
        for member in effective:
            gap = [name for name in criterion_names if name not in scored.get(member["bid_id"], set())]
            if gap:
                missing[str(member["bid_id"])] = gap

        orphan_bids = sorted(bid for bid in crashed_bids if str(bid) in missing)

        tender_status = tender["status"]
        if tender_status in {"awarded", "cancelled"} or batch["status"] == "superseded":
            determination = BATCH_FINISHED
        elif not effective:
            determination = BATCH_FINISHED if tender_status in {"awarded", "cancelled"} else BATCH_CONTINUE
        elif missing:
            determination = BATCH_REEVALUATE if (invalidated or orphan_bids) else BATCH_CONTINUE
        else:
            determination = BATCH_COMPLETE

        return {
            "batch_id": batch["id"],
            "batch_no": batch["batch_no"],
            "status": batch["status"],
            "reason": batch["reason"],
            "determination": determination,
            "member_bids": members,
            "effective_bids": effective,
            "criteria": criterion_names,
            "missing_scores": missing,
            "invalidated_scores": invalidated,
            "crashed_bids": orphan_bids,
            "award_frozen": bool(tender["award_frozen"]),
            "tender_status": tender_status,
            "complete": bool(effective) and not missing and determination != BATCH_FINISHED,
        }

    def _sync_batch(self, conn: sqlite3.Connection, tender_id: int,
                    now: str | None = None) -> dict[str, Any] | None:
        """按已写入记录回写批次状态与授标冻结位，返回进度（可在事务中调用）。"""
        now = now or utcnow()
        tender = self._tender(conn, tender_id)
        batch = self._active_batch(conn, tender)
        if batch is None:
            open_complaints = conn.execute(
                "SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)
            ).fetchone()["c"]
            frozen = 1 if open_complaints else 0
            if bool(tender["award_frozen"]) != bool(frozen):
                conn.execute("UPDATE tenders SET award_frozen=? WHERE id=?", (frozen, tender_id))
            return None
        progress = self._batch_progress(conn, batch, tender)
        new_status = "complete" if progress["complete"] else "active"
        if new_status != batch["status"]:
            conn.execute(
                "UPDATE evaluation_batches SET status=?,completed_at=? WHERE id=?",
                (new_status, now if new_status == "complete" else None, batch["id"]),
            )
            if new_status == "complete":
                self._audit(conn, tender_id, "system", "batch.completed",
                            {"batch_id": batch["id"], "batch_no": batch["batch_no"]})
            else:
                self._audit(conn, tender_id, "system", "batch.reopened",
                            {"batch_id": batch["id"], "batch_no": batch["batch_no"]})
        # 冻结位的权威来源：在办投诉；或投诉触发的新批尚未评完（旧批投诉处理结论要求重评）
        open_complaints = conn.execute(
            "SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)
        ).fetchone()["c"]
        complaint_batch_pending = batch["reason"] == "complaint" and progress["missing_scores"]
        frozen = 1 if open_complaints or complaint_batch_pending else 0
        if bool(tender["award_frozen"]) != bool(frozen):
            conn.execute("UPDATE tenders SET award_frozen=? WHERE id=?", (frozen, tender_id))
        return progress

    def recover(self) -> dict[str, Any]:
        """服务启动恢复：把崩溃残留的半批尝试标记为 superseded，再按记录判定每批。

        判定完全来自已写入记录，不删除任何数据：
        - continue：批次有缺口，但没有失效/半批残留，等评审人继续补分；
        - reevaluate：缺口由失效评分或崩溃半批造成，相关投标需重评；
        - complete：全部有效投标评分齐全；
        - finished：批次已被替代或项目已授标/取消。
        """
        report: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT t.id AS tender_id,b.id AS batch_id,b.batch_no
                   FROM tenders t JOIN evaluation_batches b ON b.id=t.active_batch_id"""
            ).fetchall()
            for row in rows:
                tender = self._tender(conn, row["tender_id"])
                batch = conn.execute("SELECT * FROM evaluation_batches WHERE id=?", (row["batch_id"],)).fetchone()
                if batch is None or batch["status"] == "superseded":
                    continue
                criterion_count = len(json.loads(batch["criteria_snapshot"]))
                orphans = conn.execute(
                    """SELECT bid_id,evaluator,COUNT(*) AS c FROM evaluations
                       WHERE batch_id=? AND score_status=?
                       GROUP BY bid_id,evaluator HAVING c>0 AND c<?""",
                    (batch["id"], SCORE_VALID, criterion_count),
                ).fetchall()
                changed = 0
                for orphan in orphans:
                    cur = conn.execute(
                        """UPDATE evaluations SET score_status=?,invalidate_reason='崩溃残留：不完整尝试',
                               updated_at=?
                           WHERE batch_id=? AND bid_id=? AND evaluator=? AND score_status=?""",
                        (SCORE_SUPERSEDED, utcnow(), batch["id"], orphan["bid_id"], orphan["evaluator"],
                         SCORE_VALID),
                    )
                    changed += cur.rowcount
                before = self._batch_progress(conn, batch, tender)["determination"]
                progress = self._sync_batch(conn, tender["id"])
                after = progress["determination"] if progress else before
                if changed or before != after:
                    self._audit(conn, tender["id"], "system", "batch.recovered",
                                {"batch_id": batch["id"], "orphan_rows": changed,
                                 "before": before, "after": after})
                report.append({"tender_id": tender["id"], "batch_id": batch["id"],
                               "batch_no": batch["batch_no"], "orphan_rows": changed,
                               "determination": after})
        return {"recovered": report}

    # ------------------------------------------------------------------
    # 主数据
    # ------------------------------------------------------------------
    def create_vendor(self, actor: str, role: str, vendor_no: str, name: str,
                      representative: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "创建供应商")
        if not vendor_no.strip() or not name.strip() or not representative.strip():
            raise DomainError("供应商编号、名称和代表不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                    (vendor_no.strip(), name.strip(), representative.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("供应商编号已存在", 409) from exc
            self._audit(conn, None, actor, "vendor.created", {"vendor_no": vendor_no.strip()})
            return dict(conn.execute("SELECT * FROM vendors WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_tender(self, actor: str, role: str, tender_no: str, title: str,
                      deadline: str, criteria: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "创建采购项目")
        parse_time(deadline)
        if not tender_no.strip() or not title.strip():
            raise DomainError("项目编号和标题不能为空")
        normalized_criteria = []
        total_weight = Decimal("0")
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("评分项格式无效")
            kind = item.get("kind", "direct")
            if kind not in {"direct", "cost"}:
                raise DomainError("评分项类型只支持 direct 或 cost")
            try:
                weight = Decimal(str(item["weight"]))
                max_value = Decimal(str(item.get("max_value", 100)))
            except (KeyError, InvalidOperation) as exc:
                raise DomainError("评分权重或上限无效") from exc
            if weight <= 0 or max_value <= 0:
                raise DomainError("评分权重和上限必须大于0")
            total_weight += weight
            normalized_criteria.append({"name": str(item["name"]).strip(), "kind": kind,
                                        "weight": float(weight), "max_value": float(max_value)})
        if not normalized_criteria or total_weight != 100:
            raise DomainError("评分项权重合计必须等于100")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO tenders(tender_no,title,description,deadline,criteria,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_no.strip(), title.strip(), description.strip(), parse_time(deadline).isoformat(timespec="seconds"),
                     json.dumps(normalized_criteria, ensure_ascii=False), actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "tender.created", {"tender_no": tender_no.strip()})
            return dict(self._tender(conn, cur.lastrowid))

    def publish_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "发布采购项目")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "draft":
                raise DomainError("只有草稿项目可以发布", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE tenders SET status='published',version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "tender.published", {"deadline": tender["deadline"]})
            return dict(self._tender(conn, tender_id))

    def submit_bid(self, actor: str, role: str, tender_id: int, vendor_id: int,
                   payload: dict[str, Any], price: float, expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "提交投标")
        if not isinstance(payload, dict):
            raise DomainError("投标内容必须是对象")
        try:
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise DomainError("报价必须是数值") from exc
        if price <= 0:
            raise DomainError("报价必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("当前项目不接受投标", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("投标截止时间已过", 409)
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            existing = conn.execute("SELECT * FROM bids WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)).fetchone()
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            digest = canonical_hash(payload)
            if existing:
                if existing["status"] != "sealed":
                    raise DomainError("投标已撤回或已开标，不能修改", 409)
                if expected_version is None or existing["version"] != int(expected_version):
                    raise DomainError("投标已变化，请刷新后重试", 409)
                conn.execute(
                    "UPDATE bids SET payload=?,payload_hash=?,price=?,version=version+1,submitted_at=? WHERE id=? AND version=?",
                    (payload_text, digest, price, utcnow(), existing["id"], expected_version),
                )
                bid_id = existing["id"]
                action = "bid.updated"
            else:
                cur = conn.execute(
                    """INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, payload_text, digest, price, actor, utcnow()),
                )
                bid_id = cur.lastrowid
                action = "bid.submitted"
            self._audit(conn, tender_id, actor, action, {"bid_id": bid_id, "vendor_id": vendor_id, "hash": digest})
            bid = dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())
            bid["payload_hash"] = digest
            return bid

    def withdraw_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "撤回投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能撤回自己的投标", 403)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]) or bid["status"] != "sealed":
                raise DomainError("截止后不能撤回投标", 409)
            conn.execute("UPDATE bids SET status='withdrawn',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.withdrawn", {"bid_id": bid_id})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def open_bids(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "开标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("项目当前不能开标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) < parse_time(tender["deadline"]):
                raise DomainError("尚未到开标时间", 409)
            rows = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status='sealed' ORDER BY id", (tender_id,)).fetchall()
            opened = []
            now = utcnow()
            for row in rows:
                digest = canonical_hash(json.loads(row["payload"]))
                if digest != row["payload_hash"]:
                    raise DomainError("投标完整性校验失败: %s" % row["id"], 409)
                conn.execute("UPDATE bids SET status='opened',opened_at=?,version=version+1 WHERE id=?", (now, row["id"]))
                opened.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (row["id"],)).fetchone()))
            conn.execute("UPDATE tenders SET status='opened',version=version+1,updated_at=? WHERE id=?", (now, tender_id))
            tender = self._tender(conn, tender_id)
            # 开标即定批：本批投标、评分项、项目版本此后固定
            self._open_batch(conn, tender, opened, "open", actor, now)
            self._sync_batch(conn, tender_id, now)
            self._audit(conn, tender_id, actor, "tender.opened", {"bid_count": len(opened)})
            return {"tender": dict(self._tender(conn, tender_id)), "bids": opened}

    # ------------------------------------------------------------------
    # 冲突 / 评分（带重试编号）/ 废标
    # ------------------------------------------------------------------
    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已结束，不能变更利益冲突", 409)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc

            batch = self._active_batch(conn, tender)
            invalidated = 0
            if batch is not None and batch["status"] != "superseded":
                if vendor_id is None:
                    target = conn.execute(
                        "SELECT id FROM bids WHERE tender_id=?", (tender_id,)
                    ).fetchall()
                else:
                    target = conn.execute(
                        "SELECT id FROM bids WHERE tender_id=? AND vendor_id=?",
                        (tender_id, vendor_id),
                    ).fetchall()
                bid_ids = [r["id"] for r in target]
                if bid_ids:
                    placeholders = ",".join("?" for _ in bid_ids)
                    result = conn.execute(
                        f"""UPDATE evaluations SET score_status=?,invalidate_reason=?,updated_at=?
                            WHERE batch_id=? AND evaluator=? AND score_status=? AND bid_id IN ({placeholders})""",
                        (SCORE_INVALIDATED, "利益冲突：" + reason.strip(), utcnow(),
                         batch["id"], evaluator.strip(), SCORE_VALID, *bid_ids),
                    )
                    invalidated = result.rowcount
                # 冲突变化后批次可能退回重评；其余评审结果继续有效
                self._sync_batch(conn, tender_id)
            self._audit(conn, tender_id, actor, "conflict.declared",
                        {"evaluator": evaluator.strip(), "vendor_id": vendor_id,
                         "reason": reason.strip(), "invalidated_scores": invalidated})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        created: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分", 409)
            batch = self._active_batch(conn, tender)
            if batch is None or batch["status"] == "superseded":
                raise DomainError("当前没有进行中的评审批次", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分", 409)
            snapshot_ids = {m["bid_id"] for m in json.loads(batch["bid_snapshot"]) if m["status"] == "eligible"}
            if bid_id not in snapshot_ids:
                raise DomainError("该投标不属于当前评审批次", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与该供应商存在利益冲突", 403)

            criteria = json.loads(batch["criteria_snapshot"])
            criteria_names = {c["name"] for c in criteria}
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            extra = [name for name in values if name not in criteria_names]
            if extra:
                raise DomainError("评分项不属于本批次: " + ",".join(extra))

            # 同一评审人对同一投标的既有尝试：完整有效 -> 拒绝覆盖；半批 -> 本体重试
            prior = conn.execute(
                "SELECT criterion,score_status FROM evaluations WHERE batch_id=? AND bid_id=? AND evaluator=?",
                (batch["id"], bid_id, actor),
            ).fetchall()
            valid_prior = {r["criterion"] for r in prior if r["score_status"] == SCORE_VALID}
            if len(valid_prior) == len(criteria):
                raise DomainError("该评分项已提交，不能覆盖", 409)
            attempt_no = conn.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM evaluations WHERE batch_id=? AND bid_id=? AND evaluator=?",
                (batch["id"], bid_id, actor),
            ).fetchone()["n"]
            if valid_prior:
                # 只替代当前仍有效的半批尝试；历史崩溃残留行保留其原因用于恢复判定
                conn.execute(
                    """UPDATE evaluations SET score_status=?,invalidate_reason='重试替代',updated_at=?
                       WHERE batch_id=? AND bid_id=? AND evaluator=? AND score_status=?""",
                    (SCORE_SUPERSEDED, utcnow(), batch["id"], bid_id, actor, SCORE_VALID),
                )

            now = utcnow()
            self._audit(conn, tender["id"], actor, "evaluation.attempt.started",
                        {"batch_id": batch["id"], "bid_id": bid_id, "attempt_no": attempt_no})
        # 每个评分项独立事务落库：服务在中途崩溃时，已写入的项就是该 attempt 的恢复依据，
        # 重启后只认同一批次，半批尝试被判定为重评而不会污染聚合。
        for criterion in criteria:
            try:
                raw = float(values[criterion["name"]])
            except (TypeError, ValueError) as exc:
                raise DomainError("评分值必须是数值") from exc
            if raw < 0 or raw > criterion["max_value"]:
                raise DomainError("评分值超出范围: " + criterion["name"])
            if criterion["kind"] == "direct":
                score = raw / criterion["max_value"] * 100
            else:
                benchmark = criterion["max_value"]
                score = min(100.0, benchmark / raw * 100) if raw > 0 else 0.0
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO evaluations(batch_id,bid_id,evaluation_round,attempt_no,evaluator,criterion,
                                              raw_value,score,score_status,comment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch["id"], bid_id, batch["batch_no"], attempt_no, actor, criterion["name"],
                     raw, score, SCORE_VALID, comment.strip(), now, now),
                )
                row = dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone())
                self._audit(conn, tender["id"], actor, "bid.evaluated",
                            {"batch_id": batch["id"], "bid_id": bid_id, "attempt_no": attempt_no,
                             "criterion": criterion["name"]})
            created.append(row)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            progress = self._sync_batch(conn, tender["id"])
        return {"bid_id": bid_id, "evaluator": actor, "batch_id": batch["id"],
                "round": batch["batch_no"], "attempt_no": attempt_no,
                "evaluations": created, "progress": progress}

    def disqualify_bid(self, actor: str, role: str, bid_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "废标")
        if not reason.strip():
            raise DomainError("废标理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("当前投标不能废标", 409)
            conn.execute("UPDATE bids SET status='disqualified',version=version+1 WHERE id=?", (bid_id,))
            batch = self._active_batch(conn, tender)
            invalidated = 0
            if batch is not None and batch["status"] != "superseded":
                # 废标只让该投标的评分失效；其他投标的结果继续使用，批次成员快照不动
                result = conn.execute(
                    "UPDATE evaluations SET score_status=?,invalidate_reason=?,updated_at=? WHERE batch_id=? AND bid_id=? AND score_status=?",
                    (SCORE_INVALIDATED, "投标废标：" + reason.strip(), utcnow(), batch["id"], bid_id, SCORE_VALID),
                )
                invalidated = result.rowcount
                self._sync_batch(conn, tender["id"])
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified",
                        {"bid_id": bid_id, "reason": reason.strip(), "invalidated_scores": invalidated})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    # ------------------------------------------------------------------
    # 澄清
    # ------------------------------------------------------------------
    def ask_clarification(self, actor: str, role: str, tender_id: int, vendor_id: int, question: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交澄清")
        if not question.strip():
            raise DomainError("澄清问题不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            cur = conn.execute(
                "INSERT INTO clarifications(tender_id,vendor_id,question,created_at) VALUES(?,?,?,?)",
                (tender_id, vendor_id, question.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "clarification.asked", {"clarification_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (cur.lastrowid,)).fetchone())

    def answer_clarification(self, actor: str, role: str, clarification_id: int,
                             answer: str, publish: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "答复澄清")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if not row:
                raise DomainError("澄清不存在", 404)
            if row["status"] != "pending":
                raise DomainError("澄清已经处理", 409)
            status = "published" if publish else "answered"
            conn.execute(
                "UPDATE clarifications SET answer=?,status=?,answered_by=?,answered_at=? WHERE id=?",
                (answer.strip(), status, actor, utcnow(), clarification_id),
            )
            self._audit(conn, row["tender_id"], actor, "clarification.answered", {"clarification_id": clarification_id, "published": publish})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    # ------------------------------------------------------------------
    # 投诉：受理即冻结授标，接受则旧批作废、另建新批
    # ------------------------------------------------------------------
    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            # 受理后先冻结授标，批次不关闭，当前评分可继续但不能授标
            conn.execute("UPDATE tenders SET award_frozen=1 WHERE id=?", (tender_id,))
            self._audit(conn, tender_id, actor, "complaint.submitted",
                        {"complaint_id": cur.lastrowid, "award_frozen": True})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())

    def resolve_complaint(self, actor: str, role: str, complaint_id: int, decision: str,
                          resolution: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "处理投诉")
        if decision not in {"accepted", "rejected"} or not resolution.strip():
            raise DomainError("投诉决定或处理说明无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if not complaint:
                raise DomainError("投诉不存在", 404)
            if complaint["status"] != "open":
                raise DomainError("投诉已经处理", 409)
            now = utcnow()
            conn.execute(
                "UPDATE complaints SET status=?,resolution=?,reviewed_by=?,resolved_at=? WHERE id=?",
                (decision, resolution.strip(), actor, now, complaint_id),
            )
            tender = self._tender(conn, complaint["tender_id"])
            new_batch = None
            if decision == "accepted":
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                old_batch = self._active_batch(conn, tender)
                if old_batch is None:
                    raise DomainError("项目尚未开标形成评审批次", 409)
                # 旧批整体封存：旧分不能参与新批
                conn.execute(
                    "UPDATE evaluation_batches SET status='superseded',superseded_at=? WHERE id=?",
                    (now, old_batch["id"]),
                )
                self._audit(conn, tender["id"], actor, "batch.superseded",
                            {"batch_id": old_batch["id"], "batch_no": old_batch["batch_no"],
                             "complaint_id": complaint_id})
                # 另建新批：成员只取仍有效的投标，评分项沿用发布时定义，重新计轮次
                live = conn.execute(
                    "SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified') ORDER BY id",
                    (tender["id"],),
                ).fetchall()
                new_batch = self._open_batch(conn, tender, live, "complaint", actor, now)
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',evaluations_locked=0,award_frozen=1,version=version+1,updated_at=? WHERE id=?",
                    (now, tender["id"]),
                )
            else:
                # 驳回后由统一规则决定是否解冻：无其他在办投诉且投诉批已评完即解冻
                self._sync_batch(conn, tender["id"], now)
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved",
                        {"complaint_id": complaint_id, "decision": decision,
                         "new_batch_id": new_batch["id"] if new_batch else None})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())

    # ------------------------------------------------------------------
    # 授标：批次评分不全 / 冻结 / 旧批一律拒绝
    # ------------------------------------------------------------------
    def award_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "授标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能授标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            open_complaint = conn.execute(
                "SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)
            ).fetchone()["c"]
            if open_complaint or tender["award_frozen"]:
                raise DomainError("存在受理中的投诉，授标已冻结", 409)
            batch = self._active_batch(conn, tender)
            if batch is None or batch["status"] == "superseded":
                raise DomainError("没有有效的评审批次，不能授标", 409)
            progress = self._batch_progress(conn, batch, tender)
            if progress["missing_scores"]:
                first = next(iter(progress["missing_scores"].items()))
                raise DomainError("当前批次评分不全，不能授标（投标 %s 缺 %s）" % (first[0], ",".join(first[1])), 409)
            if not progress["effective_bids"]:
                raise DomainError("没有可授标的有效投标", 409)

            criteria = json.loads(batch["criteria_snapshot"])
            ranking = []
            # 并发授标防护：只取当前批的有效评分，旧批/失效行/重试旧行一律读不到
            for member in progress["effective_bids"]:
                bid_id = member["bid_id"]
                rows = conn.execute(
                    """SELECT criterion,AVG(score) AS score FROM evaluations
                       WHERE batch_id=? AND bid_id=? AND score_status=?
                       GROUP BY criterion""",
                    (batch["id"], bid_id, SCORE_VALID),
                ).fetchall()
                scores = {row["criterion"]: row["score"] for row in rows}
                weighted = 0.0
                for criterion in criteria:
                    weighted += scores[criterion["name"]] * criterion["weight"] / 100
                bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
                ranking.append({"bid_id": bid_id, "vendor_id": bid["vendor_id"],
                                "price": bid["price"], "score": round(weighted, 2)})
            ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
            winner = ranking[0]
            now = utcnow()
            snapshot = {"tender_id": tender_id, "batch_id": batch["id"],
                        "round": batch["batch_no"], "ranking": ranking,
                        "winner": winner, "awarded_by": actor, "awarded_at": now}
            conn.execute(
                "UPDATE evaluation_batches SET status='complete',completed_at=? WHERE id=?",
                (now, batch["id"]),
            )
            cur = conn.execute(
                "UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,evaluations_locked=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), now, tender_id, expected_version),
            )
            if cur.rowcount != 1:
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            self._audit(conn, tender_id, actor, "tender.awarded",
                        {"batch_id": batch["id"], "winner": winner, "ranking": ranking})
            return {"tender": dict(self._tender(conn, tender_id)), "award": snapshot}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def batch_state(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            batch = self._active_batch(conn, tender)
            if batch is None:
                return {"tender_id": tender_id, "active_batch": None,
                        "award_frozen": bool(tender["award_frozen"])}
            progress = self._batch_progress(conn, batch, self._tender(conn, tender_id))
            history = [dict(r) for r in conn.execute(
                "SELECT id,batch_no,status,reason,tender_version,created_at,completed_at,superseded_at FROM evaluation_batches WHERE tender_id=? ORDER BY batch_no",
                (tender_id,),
            ).fetchall()]
            for item in history:
                item["bid_snapshot"] = json.loads(
                    conn.execute("SELECT bid_snapshot FROM evaluation_batches WHERE id=?", (item["id"],)).fetchone()["bid_snapshot"]
                )
            progress["history"] = history
            return progress

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender_row = self._tender(conn, tender_id)
            tender = dict(tender_row)
            batch = self._active_batch(conn, tender_row)
            tender["active_batch"] = {"id": batch["id"], "batch_no": batch["batch_no"],
                                      "status": batch["status"], "reason": batch["reason"]} if batch else None
            bids = []
            if role in {"procurement", "supervisor", "auditor"} and tender["status"] in {"opened", "reevaluation", "awarded"}:
                bids = [dict(r) for r in conn.execute("SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    "SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id WHERE b.tender_id=? AND b.submitted_by=?",
                    (tender_id, actor),
                ).fetchall():
                    item = dict(row)
                    item.pop("tender_status", None)
                    if tender["status"] not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
            else:
                bids = [dict(r) for r in conn.execute(
                    "SELECT id,tender_id,vendor_id,price,status,payload_hash,submitted_at,opened_at FROM bids WHERE tender_id=? ORDER BY id",
                    (tender_id,),
                ).fetchall()]
            clarifications = [dict(r) for r in conn.execute(
                "SELECT id,tender_id,vendor_id,question,answer,status,answered_at FROM clarifications WHERE tender_id=? AND status='published' ORDER BY id",
                (tender_id,),
            ).fetchall()]
            result = {"tender": tender, "bids": bids, "clarifications": clarifications}
            if batch is not None and role in {"procurement", "supervisor", "auditor", "evaluator"}:
                result["active_batch_progress"] = self._batch_progress(conn, batch, tender_row)
            return result

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            tenders = [dict(r) for r in conn.execute(
                """SELECT id,tender_no,title,description,status,deadline,evaluation_round,version,
                          award_frozen,active_batch_id,awarded_bid_id,created_at,updated_at
                   FROM tenders ORDER BY id DESC"""
            ).fetchall()]
            batches = [dict(r) for r in conn.execute(
                "SELECT id,tender_id,batch_no,status,reason,created_at,completed_at,superseded_at FROM evaluation_batches ORDER BY id DESC LIMIT 200"
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            if role in {"procurement", "supervisor", "auditor"}:
                bids = [dict(r) for r in conn.execute(
                    """SELECT b.id,b.tender_id,b.vendor_id,b.price,b.status,b.payload_hash,b.submitted_at,b.opened_at,
                              CASE WHEN t.status IN ('opened','reevaluation','awarded') THEN b.payload ELSE NULL END AS payload
                       FROM bids b JOIN tenders t ON t.id=b.tender_id ORDER BY b.id DESC LIMIT 200"""
                ).fetchall()]
                complaints = [dict(r) for r in conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 100").fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    """SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id
                       WHERE b.submitted_by=? ORDER BY b.id DESC LIMIT 100""",
                    (actor,),
                ).fetchall():
                    item = dict(row)
                    status = item.pop("tender_status")
                    if status not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
                complaints = [dict(r) for r in conn.execute(
                    "SELECT * FROM complaints WHERE complainant=? ORDER BY id DESC LIMIT 100", (actor,)
                ).fetchall()]
            else:
                bids, complaints = [], []
        return {"tenders": tenders, "bids": bids, "batches": batches, "complaints": complaints,
                "timeline": timeline, "role": role}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM tenders").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        vendor = self.create_vendor("proc-demo", "procurement", "V-001", "启明科技", "vendor-demo")
        deadline = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")
        tender = self.create_tender(
            "proc-demo", "procurement", "TENDER-DEMO", "服务器采购", deadline,
            [{"name": "价格", "weight": 60, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}],
        )
        published = self.publish_tender("proc-demo", "procurement", tender["id"], tender["version"])
        self.submit_bid("vendor-demo", "vendor", tender["id"], vendor["id"], {"价格": 900000, "质量": 90}, 900000)
        return {"seeded": True, "tender_id": tender["id"], "vendor_id": vendor["id"], "published_version": published["version"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: ProcurementService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "public")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "public-procurement"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path.endswith("/batch") and path.startswith("/api/tenders/"):
                tender_id = int(path.split("/")[3])
                self._send(200, self.service.batch_state(actor, role, tender_id))
            elif path.startswith("/api/tenders/"):
                self._send(200, self.service.get_tender(actor, role, int(path.split("/")[3])))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/vendors":
                result = self.service.create_vendor(actor, role, **data)
            elif path == "/api/tenders":
                result = self.service.create_tender(actor, role, **data)
            elif path == "/api/tenders/publish":
                result = self.service.publish_tender(actor, role, **data)
            elif path == "/api/bids":
                result = self.service.submit_bid(actor, role, **data)
            elif path == "/api/bids/withdraw":
                result = self.service.withdraw_bid(actor, role, **data)
            elif path == "/api/tenders/open":
                result = self.service.open_bids(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/evaluations":
                result = self.service.evaluate_bid(actor, role, **data)
            elif path == "/api/bids/disqualify":
                result = self.service.disqualify_bid(actor, role, **data)
            elif path == "/api/clarifications":
                result = self.service.ask_clarification(actor, role, **data)
            elif path == "/api/clarifications/answer":
                result = self.service.answer_clarification(actor, role, **data)
            elif path == "/api/complaints":
                result = self.service.submit_complaint(actor, role, **data)
            elif path == "/api/complaints/resolve":
                result = self.service.resolve_complaint(actor, role, **data)
            elif path == "/api/tenders/award":
                result = self.service.award_tender(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: ProcurementService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Public procurement service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="公共采购密封投标服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8209)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = ProcurementService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
