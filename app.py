"""Sealed public-procurement tendering and evaluation service.

评审状态链：采购项目(tenders) -> 开标/投诉产生评分批次(evaluation_batches)
-> 批次固定投标名册(batch_bids，含投标版本) -> 带重试编号的评分(evaluations)
-> 批次完成 -> 授标快照。

关键规则：
- 每批固定参与投标、评分项快照与项目版本；评分必须落在项目当前批次。
- 评分带 attempt_no：同一 attempt 重试只补齐崩溃残留，不产生重复行；新 attempt 作废旧 attempt。
- 利益冲突变化或投标被废标，只把相关评分置为失效（valid=0），其余结果继续使用。
- 投诉受理立即冻结授标并另建批次，旧批次评分不参与新批次；驳回则撤销新批次、旧批继续。
- 授标前在同一事务内重算批次有效性，评分不全（批次未完成）不得授标；版本号守卫并发授标。
- 重启后仅凭已写入记录重放判定：批次继续(continue)/重评(reassess)/可授标(ready)/冻结(frozen)/结束(ended)。
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

ACTIVE_BID_STATUSES = ("opened", "qualified")
BID_STATUSES_IN_SCOPE = ("opened", "qualified", "awarded")
CRITERION_KINDS = {"direct", "cost"}


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


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()
        # 服务重启：仅凭已写入记录重放批次状态（幂等）。
        self.recover_all()

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
                    current_batch_id INTEGER,
                    award_frozen INTEGER NOT NULL DEFAULT 0,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
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
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    criteria TEXT NOT NULL,
                    origin_tender_version INTEGER NOT NULL,
                    origin_complaint_id INTEGER,
                    activated_at TEXT,
                    completed_at TEXT,
                    awarded_at TEXT,
                    cancelled_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,batch_no)
                );
                CREATE TABLE IF NOT EXISTS batch_bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES evaluation_batches(id),
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    bid_version INTEGER NOT NULL,
                    required INTEGER NOT NULL DEFAULT 1,
                    excluded_reason TEXT,
                    UNIQUE(batch_id,bid_id)
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
                    comment TEXT NOT NULL DEFAULT '',
                    valid INTEGER NOT NULL DEFAULT 1,
                    invalid_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id,bid_id,evaluator,criterion,attempt_no)
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
                CREATE INDEX IF NOT EXISTS idx_batches_tender ON evaluation_batches(tender_id,batch_no);
                CREATE INDEX IF NOT EXISTS idx_batch_bids_batch ON batch_bids(batch_id);
                CREATE INDEX IF NOT EXISTS idx_eval_batch ON evaluations(batch_id,bid_id,valid);
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把仅带 evaluation_round 的旧库补成批次链（best-effort，保留旧评分）。"""
        tender_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tenders)").fetchall()}
        if "current_batch_id" not in tender_cols:
            conn.execute("ALTER TABLE tenders ADD COLUMN current_batch_id INTEGER")
        if "award_frozen" not in tender_cols:
            conn.execute("ALTER TABLE tenders ADD COLUMN award_frozen INTEGER NOT NULL DEFAULT 0")
        eval_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evaluations)").fetchall()}
        if eval_cols and "batch_id" not in eval_cols:
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
                    comment TEXT NOT NULL DEFAULT '',
                    valid INTEGER NOT NULL DEFAULT 1,
                    invalid_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id,bid_id,evaluator,criterion,attempt_no)
                );
                CREATE INDEX IF NOT EXISTS idx_eval_batch ON evaluations(batch_id,bid_id,valid);
                """
            )
            now = utcnow()
            legacy = conn.execute(
                """SELECT e.*, b.tender_id AS tender_id FROM evaluations_legacy e
                   JOIN bids b ON b.id=e.bid_id"""
            ).fetchall()
            batch_ids: dict[tuple[int, int], int] = {}
            for row in legacy:
                key = (row["tender_id"], row["evaluation_round"])
                if key in batch_ids:
                    continue
                tender = conn.execute("SELECT * FROM tenders WHERE id=?", (key[0],)).fetchone()
                cur = conn.execute(
                    """INSERT INTO evaluation_batches(tender_id,batch_no,reason,status,criteria,
                          origin_tender_version,activated_at,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (key[0], key[1], "initial",
                     "awarded" if tender and tender["status"] == "awarded" else "open",
                     tender["criteria"] if tender else "[]",
                     tender["version"] if tender else 1, now, "migration", now),
                )
                batch_ids[key] = cur.lastrowid
                for bid in conn.execute(
                    "SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified','awarded','disqualified')",
                    (key[0],),
                ).fetchall():
                    active = bid["status"] in ("opened", "qualified", "awarded")
                    conn.execute(
                        "INSERT INTO batch_bids(batch_id,bid_id,bid_version,required,excluded_reason) VALUES(?,?,?,?,?)",
                        (cur.lastrowid, bid["id"], bid["version"], 1 if active else 0,
                         None if active else "bid_inactive"),
                    )
            for row in legacy:
                conn.execute(
                    """INSERT INTO evaluations(batch_id,bid_id,evaluation_round,attempt_no,evaluator,
                          criterion,raw_value,score,comment,valid,created_at,updated_at)
                       VALUES(?,?,?,1,?,?,?,?,?,1,?,?)""",
                    (batch_ids[(row["tender_id"], row["evaluation_round"])], row["bid_id"],
                     row["evaluation_round"], row["evaluator"], row["criterion"], row["raw_value"],
                     row["score"], row["comment"], row["created_at"], row["updated_at"]),
                )
            conn.execute("DROP TABLE evaluations_legacy")
            for tender in conn.execute("SELECT id FROM tenders").fetchall():
                latest = conn.execute(
                    "SELECT id,batch_no FROM evaluation_batches WHERE tender_id=? ORDER BY batch_no DESC LIMIT 1",
                    (tender["id"],),
                ).fetchone()
                if latest:
                    conn.execute(
                        "UPDATE tenders SET current_batch_id=?,evaluation_round=? WHERE id=?",
                        (latest["id"], latest["batch_no"], tender["id"]),
                    )

    # ---------- 基础工具 ----------

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

    def _batch(self, conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM evaluation_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise DomainError("评分批次不存在", 404)
        return row

    def _has_conflict(self, conn: sqlite3.Connection, tender_id: int,
                      evaluator: str, vendor_id: int) -> bool:
        for row in conn.execute(
            "SELECT vendor_id FROM conflicts WHERE tender_id=? AND evaluator=?",
            (tender_id, evaluator),
        ).fetchall():
            if row["vendor_id"] is None or row["vendor_id"] == vendor_id:
                return True
        return False

    # ---------- 批次状态链核心 ----------

    def _create_batch(self, conn: sqlite3.Connection, tender: sqlite3.Row, reason: str,
                      actor: str, complaint_id: int | None = None) -> sqlite3.Row:
        """开新批次：固定批次号、评分项快照、项目版本与当前有效投标名册。"""
        now = utcnow()
        batch_no = conn.execute(
            "SELECT COALESCE(MAX(batch_no),0)+1 AS n FROM evaluation_batches WHERE tender_id=?",
            (tender["id"],),
        ).fetchone()["n"]
        cur = conn.execute(
            """INSERT INTO evaluation_batches(tender_id,batch_no,reason,status,criteria,
                  origin_tender_version,origin_complaint_id,activated_at,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (tender["id"], batch_no, reason, "open", tender["criteria"], tender["version"],
             complaint_id, now if reason == "initial" else None, actor, now),
        )
        batch_id = cur.lastrowid
        bids = conn.execute(
            "SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified') ORDER BY id",
            (tender["id"],),
        ).fetchall()
        for bid in bids:  # 固定参与投标及其版本
            conn.execute(
                "INSERT INTO batch_bids(batch_id,bid_id,bid_version,required) VALUES(?,?,?,1)",
                (batch_id, bid["id"], bid["version"]),
            )
        conn.execute(
            "UPDATE tenders SET current_batch_id=?,evaluation_round=?,version=version+1,updated_at=? WHERE id=?",
            (batch_id, batch_no, now, tender["id"]),
        )
        self._audit(conn, tender["id"], actor, "evaluation_batch.created",
                    {"batch_id": batch_id, "batch_no": batch_no, "reason": reason,
                     "bid_ids": [b["id"] for b in bids], "complaint_id": complaint_id})
        return self._refresh_batch(conn, batch_id)

    def _refresh_batch(self, conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
        """按已写入记录重算：废标/冲突/重试导致的评分失效，以及批次是否完成。幂等。"""
        batch = conn.execute("SELECT * FROM evaluation_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None or batch["status"] in ("awarded", "cancelled"):
            return batch
        now = utcnow()

        roster = conn.execute("SELECT * FROM batch_bids WHERE batch_id=?", (batch_id,)).fetchall()
        bid_rows = {r["bid_id"]: conn.execute("SELECT * FROM bids WHERE id=?", (r["bid_id"],)).fetchone()
                    for r in roster}
        # 废标（或撤回/授标他用）后不再要求该投标的评分；名册保留以固定历史参与方。
        for item in roster:
            bid = bid_rows[item["bid_id"]]
            if item["required"] and (bid is None or bid["status"] not in BID_STATUSES_IN_SCOPE):
                conn.execute(
                    "UPDATE batch_bids SET required=0,excluded_reason='bid_inactive' WHERE id=?",
                    (item["id"],),
                )

        evals = conn.execute(
            "SELECT * FROM evaluations WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
        latest_attempt: dict[tuple[int, str], int] = {}
        for e in evals:
            key = (e["bid_id"], e["evaluator"])
            latest_attempt[key] = max(latest_attempt.get(key, 0), e["attempt_no"])
        for e in evals:
            bid = bid_rows.get(e["bid_id"])
            if bid is not None and bid["status"] not in BID_STATUSES_IN_SCOPE:
                valid, reason = 0, "bid_inactive"
            elif bid is not None and self._has_conflict(conn, batch["tender_id"], e["evaluator"], bid["vendor_id"]):
                valid, reason = 0, "conflict"
            elif e["attempt_no"] < latest_attempt[(e["bid_id"], e["evaluator"])]:
                # 新重试一旦开始，旧重试该评审人对该投标的全部评分作废，防止混用不同 attempt。
                valid, reason = 0, "superseded"
            else:
                valid, reason = 1, None
            if e["valid"] != valid or e["invalid_reason"] != reason:
                conn.execute(
                    "UPDATE evaluations SET valid=?,invalid_reason=?,updated_at=? WHERE id=?",
                    (valid, reason, now, e["id"]),
                )

        criteria = [c["name"] for c in json.loads(batch["criteria"])]
        required_ids = [r["bid_id"] for r in conn.execute(
            "SELECT bid_id FROM batch_bids WHERE batch_id=? AND required=1", (batch_id,)).fetchall()]
        covered = self._covered_cells(conn, batch_id, required_ids, criteria)
        complete = bool(required_ids) and all(
            (bid_id, name) in covered for bid_id in required_ids for name in criteria
        )
        new_status = "completed" if complete else "open"
        if new_status != batch["status"]:
            conn.execute(
                "UPDATE evaluation_batches SET status=?,completed_at=? WHERE id=?",
                (new_status, now if complete else None, batch_id),
            )
        return conn.execute("SELECT * FROM evaluation_batches WHERE id=?", (batch_id,)).fetchone()

    @staticmethod
    def _covered_cells(conn: sqlite3.Connection, batch_id: int,
                       bid_ids: list[int], criteria: list[str]) -> set[tuple[int, str]]:
        """仍有效的最新评分覆盖到的 (投标, 评分项) 单元。"""
        if not bid_ids:
            return set()
        placeholders = ",".join("?" for _ in bid_ids)
        rows = conn.execute(
            f"""SELECT bid_id,criterion FROM evaluations
                WHERE batch_id=? AND valid=1 AND bid_id IN ({placeholders})
                GROUP BY bid_id,criterion""",
            [batch_id, *bid_ids],
        ).fetchall()
        return {(r["bid_id"], r["criterion"]) for r in rows}

    def _derive_tender_status(self, pointer: sqlite3.Row | None, current: str) -> str:
        if current in ("awarded", "cancelled") or pointer is None:
            return current
        if pointer["reason"] == "complaint" and pointer["activated_at"]:
            return "reevaluation"
        if pointer["batch_no"] == 1:
            return "opened"
        return "reevaluation"

    def _reconcile_tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        """重放项目：冻结标志、各批次失效/完成、当前批次指针与项目状态。幂等。"""
        tender = self._tender(conn, tender_id)
        open_complaints = conn.execute(
            "SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)
        ).fetchone()["c"]
        frozen = 1 if open_complaints else 0
        batches = conn.execute(
            "SELECT * FROM evaluation_batches WHERE tender_id=? ORDER BY batch_no", (tender_id,)
        ).fetchall()
        for b in batches:
            if b["status"] not in ("awarded", "cancelled"):
                self._refresh_batch(conn, b["id"])
        pointer = conn.execute(
            "SELECT * FROM evaluation_batches WHERE tender_id=? AND status!='cancelled' ORDER BY batch_no DESC LIMIT 1",
            (tender_id,),
        ).fetchone()
        if tender["status"] not in ("awarded", "cancelled"):
            status = self._derive_tender_status(pointer, tender["status"])
            conn.execute(
                "UPDATE tenders SET award_frozen=?,status=? WHERE id=?",
                (frozen, status, tender_id),
            )
            if pointer:
                conn.execute(
                    "UPDATE tenders SET current_batch_id=?,evaluation_round=? WHERE id=?",
                    (pointer["id"], pointer["batch_no"], tender_id),
                )
        else:
            conn.execute("UPDATE tenders SET award_frozen=? WHERE id=?", (frozen, tender_id))
        return self._tender(conn, tender_id)

    def _batch_decision(self, conn: sqlite3.Connection, tender: sqlite3.Row,
                        batch: sqlite3.Row) -> dict[str, Any]:
        """凭记录判断这批：继续 / 重评 / 可授标 / 冻结 / 结束。"""
        criteria = [c["name"] for c in json.loads(batch["criteria"])]
        required = conn.execute(
            "SELECT bid_id FROM batch_bids WHERE batch_id=? AND required=1", (batch["id"],)
        ).fetchall()
        required_ids = [r["bid_id"] for r in required]
        covered = self._covered_cells(conn, batch["id"], required_ids, criteria)
        missing = [(bid_id, name) for bid_id in required_ids for name in criteria
                   if (bid_id, name) not in covered]
        invalidated = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE batch_id=? AND valid=0", (batch["id"],)
        ).fetchone()["c"]
        if tender["status"] == "awarded" or batch["status"] == "awarded":
            decision = "ended"
        elif batch["status"] == "cancelled":
            decision = "cancelled"
        elif tender["award_frozen"]:
            decision = "frozen"
        elif batch["status"] == "completed":
            decision = "ready"
        elif invalidated:
            decision = "reassess"
        else:
            decision = "continue"
        return {
            "decision": decision,
            "required_cells": len(required_ids) * len(criteria),
            "covered_cells": len(required_ids) * len(criteria) - len(missing),
            "missing_cells": [{"bid_id": bid_id, "criterion": name} for bid_id, name in missing],
            "invalidated_evaluations": invalidated,
        }

    def _batch_view(self, conn: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
        roster = []
        for item in conn.execute(
            """SELECT bb.*, b.vendor_id AS vendor_id, b.status AS bid_status
               FROM batch_bids bb JOIN bids b ON b.id=bb.bid_id WHERE bb.batch_id=? ORDER BY bb.id""",
            (batch["id"],),
        ).fetchall():
            roster.append({"bid_id": item["bid_id"], "vendor_id": item["vendor_id"],
                           "bid_version": item["bid_version"], "required": bool(item["required"]),
                           "excluded_reason": item["excluded_reason"], "bid_status": item["bid_status"]})
        counts = conn.execute(
            "SELECT valid,COUNT(*) AS c FROM evaluations WHERE batch_id=? GROUP BY valid", (batch["id"],)
        ).fetchall()
        valid_count = sum(r["c"] for r in counts if r["valid"])
        invalid_count = sum(r["c"] for r in counts if not r["valid"])
        return {
            "id": batch["id"], "batch_no": batch["batch_no"], "reason": batch["reason"],
            "status": batch["status"], "criteria": json.loads(batch["criteria"]),
            "origin_tender_version": batch["origin_tender_version"],
            "origin_complaint_id": batch["origin_complaint_id"],
            "activated_at": batch["activated_at"], "completed_at": batch["completed_at"],
            "awarded_at": batch["awarded_at"], "cancelled_at": batch["cancelled_at"],
            "created_at": batch["created_at"], "roster": roster,
            "valid_evaluations": valid_count, "invalid_evaluations": invalid_count,
        }

    def recover_all(self) -> dict[str, Any]:
        """服务启动恢复：重放全部未结束项目的批次链。"""
        results = []
        with self.connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM tenders ORDER BY id").fetchall()]
        for tender_id in ids:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                tender = self._tender(conn, tender_id)
                if tender["status"] not in ("awarded", "cancelled") and tender["current_batch_id"] is None \
                        and tender["status"] in ("opened", "reevaluation"):
                    # 安全兜底：已开标但无批次（异常/旧库），用当前有效投标补建初始批。
                    self._create_batch(conn, tender, "initial", "recovery")
                tender = self._reconcile_tender(conn, tender_id)
                batch = conn.execute(
                    "SELECT * FROM evaluation_batches WHERE id=?", (tender["current_batch_id"],)
                ).fetchone() if tender["current_batch_id"] else None
                if batch:
                    view = self._batch_view(conn, batch)
                    view.update(self._batch_decision(conn, tender, batch))
                    results.append(view)
        return {"recovered_batches": results}

    def recover_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor", "auditor"}, "恢复判定")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._reconcile_tender(conn, tender_id)
            batches = [self._batch_view(conn, b) for b in conn.execute(
                "SELECT * FROM evaluation_batches WHERE tender_id=? ORDER BY batch_no", (tender_id,)
            ).fetchall()]
            current = None
            if tender["current_batch_id"]:
                current = self._batch_view(conn, self._batch(conn, tender["current_batch_id"]))
                current.update(self._batch_decision(
                    conn, tender, self._batch(conn, tender["current_batch_id"])))
            self._audit(conn, tender_id, actor, "tender.recovered",
                        {"current_batch_id": tender["current_batch_id"]})
            return {"tender_id": tender_id, "status": tender["status"],
                    "award_frozen": bool(tender["award_frozen"]),
                    "current_batch": current, "batches": batches}

    # ---------- 业务接口 ----------

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
            if kind not in CRITERION_KINDS:
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
            if conn.execute(
                "SELECT COUNT(*) AS c FROM evaluation_batches WHERE tender_id=?", (tender_id,)
            ).fetchone()["c"]:
                raise DomainError("项目已开过标，批次已存在", 409)
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
            batch = self._create_batch(conn, tender, "initial", actor)  # 首批固定名册与评分项
            self._audit(conn, tender_id, actor, "tender.opened",
                        {"bid_count": len(opened), "batch_id": batch["id"]})
            return {"tender": dict(self._tender(conn, tender_id)), "bids": opened,
                    "batch": self._batch_view(conn, batch)}

    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._tender(conn, tender_id)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc
            for b in conn.execute(
                "SELECT id FROM evaluation_batches WHERE tender_id=? AND status NOT IN ('awarded','cancelled')",
                (tender_id,),
            ).fetchall():
                self._refresh_batch(conn, b["id"])
            self._reconcile_tender(conn, tender_id)
            conn.execute("UPDATE tenders SET version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "conflict.declared",
                        {"evaluator": evaluator.strip(), "vendor_id": vendor_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def retract_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None = None) -> dict[str, Any]:
        """冲突消除：相关失效评分不会自动复活（需按新 attempt 重评），其余评分继续使用。"""
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "撤销利益冲突")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._tender(conn, tender_id)
            if vendor_id is None:
                row = conn.execute(
                    "SELECT * FROM conflicts WHERE tender_id=? AND evaluator=? AND vendor_id IS NULL ORDER BY id LIMIT 1",
                    (tender_id, evaluator.strip()),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM conflicts WHERE tender_id=? AND evaluator=? AND vendor_id=? ORDER BY id LIMIT 1",
                    (tender_id, evaluator.strip(), vendor_id),
                ).fetchone()
            if not row:
                raise DomainError("利益冲突记录不存在", 404)
            if row["declared_by"] != actor and role not in {"procurement", "supervisor"}:
                raise DomainError("只能撤销本人申报的冲突", 403)
            conn.execute("DELETE FROM conflicts WHERE id=?", (row["id"],))
            for b in conn.execute(
                "SELECT id FROM evaluation_batches WHERE tender_id=? AND status NOT IN ('awarded','cancelled')",
                (tender_id,),
            ).fetchall():
                self._refresh_batch(conn, b["id"])
            self._reconcile_tender(conn, tender_id)
            conn.execute("UPDATE tenders SET version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "conflict.retracted",
                        {"evaluator": evaluator.strip(), "vendor_id": vendor_id})
            return {"retracted": True, "conflict_id": row["id"]}

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     attempt_no: int = 1, comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        try:
            attempt_no = int(attempt_no)
        except (TypeError, ValueError) as exc:
            raise DomainError("重试编号必须是整数") from exc
        if attempt_no < 1:
            raise DomainError("重试编号必须从1开始")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能评分", 409)
            if tender["award_frozen"]:
                raise DomainError("投诉受理中，授标与评分已冻结，须在新批次开始后重评", 409)
            if tender["evaluations_locked"]:
                raise DomainError("评分已锁定", 409)
            if not tender["current_batch_id"]:
                raise DomainError("当前项目没有进行中的评分批次", 409)
            batch = self._batch(conn, tender["current_batch_id"])
            if batch["status"] != "open":
                raise DomainError("当前评分批次不是进行中状态", 409)
            if batch["reason"] == "complaint" and not batch["activated_at"]:
                raise DomainError("新批次尚未激活，等待投诉处理结果", 409)
            entry = conn.execute(
                "SELECT * FROM batch_bids WHERE batch_id=? AND bid_id=?", (batch["id"], bid_id)
            ).fetchone()
            if not entry or not entry["required"]:
                raise DomainError("该投标不属于本评分批次的有效名册", 409)
            if bid["version"] != entry["bid_version"]:
                raise DomainError("投标版本与批次固定版本不一致，请刷新后在当前批次重评", 409)
            if bid["status"] not in ACTIVE_BID_STATUSES:
                raise DomainError("该投标当前状态不能评分", 409)
            if self._has_conflict(conn, tender["id"], actor, bid["vendor_id"]):
                raise DomainError("评审人与该供应商存在利益冲突", 403)

            criteria = json.loads(batch["criteria"])  # 批次固定的评分项快照
            names = {c["name"] for c in criteria}
            extra = [k for k in values if k not in names]
            if extra:
                raise DomainError("存在批次外评分项: " + ",".join(extra))

            prior = conn.execute(
                "SELECT criterion,attempt_no FROM evaluations WHERE batch_id=? AND bid_id=? AND evaluator=?",
                (batch["id"], bid_id, actor),
            ).fetchall()
            max_attempt = max((r["attempt_no"] for r in prior), default=0)
            same_attempt = {r["criterion"] for r in prior if r["attempt_no"] == attempt_no}
            if attempt_no > max_attempt + 1:
                raise DomainError("重试编号不连续：本批评分应为第 %d 次" % (max_attempt + 1), 409)
            if not values:
                raise DomainError("至少提交一个评分项")
            created, reused = [], bool(same_attempt)
            if same_attempt >= names:
                raise DomainError("该重试编号的评分已完整提交，不能覆盖；重评请使用新的重试编号", 409)
            # 崩溃恢复：同一 attempt 允许部分写入后补齐；服务重启后凭已写入行识别续评。

            now = utcnow()
            for criterion in criteria:
                if criterion["name"] not in values:
                    continue  # 崩溃后部分写入：允许分次补齐同一 attempt
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
                if criterion["name"] in same_attempt:
                    continue  # 已写入项保持不变
                cur = conn.execute(
                    """INSERT INTO evaluations(batch_id,bid_id,evaluation_round,attempt_no,evaluator,
                          criterion,raw_value,score,comment,valid,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (batch["id"], bid_id, batch["batch_no"], attempt_no, actor,
                     criterion["name"], raw, score, comment.strip(), now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))

            batch = self._refresh_batch(conn, batch["id"])
            self._audit(conn, tender["id"], actor, "bid.evaluated",
                        {"batch_id": batch["id"], "bid_id": bid_id, "attempt_no": attempt_no,
                         "retry_recovered": reused, "criteria": [item["criterion"] for item in created],
                         "batch_status": batch["status"]})
            return {"bid_id": bid_id, "evaluator": actor, "batch_id": batch["id"],
                    "round": batch["batch_no"], "attempt_no": attempt_no,
                    "retry_recovered": reused, "batch_status": batch["status"], "evaluations": created}

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
            # 该投标在各未结束批次中的评分失效，其余投标结果继续使用。
            self._reconcile_tender(conn, tender["id"])
            conn.execute("UPDATE tenders SET version=version+1,updated_at=? WHERE id=?", (utcnow(), tender["id"]))
            current = self._batch(conn, tender["current_batch_id"]) if tender["current_batch_id"] else None
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified",
                        {"bid_id": bid_id, "reason": reason.strip(),
                         "batch_status": current["status"] if current else None})
            return {"bid": dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()),
                    "batch": self._batch_view(conn, current) if current else None}

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

    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        """投诉受理：立即冻结授标与评分，并另建批次；旧批评分不得进入新批。"""
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("项目尚未开标，不能投诉评审结果", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            complaint_id = cur.lastrowid
            frozen_batch = self._create_batch(conn, tender, "complaint", actor, complaint_id)
            conn.execute("UPDATE tenders SET award_frozen=1 WHERE id=?", (tender_id,))
            self._audit(conn, tender_id, actor, "complaint.submitted",
                        {"complaint_id": complaint_id, "frozen_batch_id": frozen_batch["id"]})
            result = dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())
            result["frozen_batch"] = self._batch_view(conn, frozen_batch)
            return result

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
            batch = conn.execute(
                "SELECT * FROM evaluation_batches WHERE origin_complaint_id=?", (complaint_id,)
            ).fetchone()
            tender = self._tender(conn, complaint["tender_id"])
            if decision == "accepted":
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                if batch:
                    # 激活新批次：旧批分数不迁移，全部按空批重评。
                    conn.execute(
                        "UPDATE evaluation_batches SET activated_at=? WHERE id=?", (now, batch["id"])
                    )
                    self._refresh_batch(conn, batch["id"])
                self._reconcile_tender(conn, tender["id"])
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',version=version+1,updated_at=? WHERE id=?",
                    (now, tender["id"]),
                )
            else:
                if batch:
                    # 驳回：撤销受理时另建的批次，指针回到旧批，旧分继续使用。
                    conn.execute(
                        "UPDATE evaluation_batches SET status='cancelled',cancelled_at=? WHERE id=?",
                        (now, batch["id"]),
                    )
                self._reconcile_tender(conn, tender["id"])
                tender = self._tender(conn, tender["id"])
                conn.execute(
                    "UPDATE tenders SET status=?,version=version+1,updated_at=? WHERE id=?",
                    (self._derive_tender_status(
                        self._batch(conn, tender["current_batch_id"]) if tender["current_batch_id"] else None,
                        tender["status"]),
                        now, tender["id"]),
                )
            tender = self._reconcile_tender(conn, tender["id"])
            current = self._batch(conn, tender["current_batch_id"]) if tender["current_batch_id"] else None
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved",
                        {"complaint_id": complaint_id, "decision": decision,
                         "current_batch_id": current["id"] if current else None})
            result = dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())
            result["current_batch"] = self._batch_view(conn, current) if current else None
            return result

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
            if tender["award_frozen"]:
                raise DomainError("投诉受理中，授标已冻结", 409)
            open_complaint = conn.execute(
                "SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)
            ).fetchone()["c"]
            if open_complaint:
                raise DomainError("存在未处理投诉，不能授标", 409)
            if not tender["current_batch_id"]:
                raise DomainError("没有可授标的评分批次", 409)
            # 授标瞬间重算：废标/冲突造成的失效必须反映，绝不混用旧分。
            batch = self._refresh_batch(conn, tender["current_batch_id"])
            if batch["status"] != "completed":
                decision_info = self._batch_decision(conn, tender, batch)
                raise DomainError(
                    "评分批次尚未完成，不能授标（状态：%s，缺 %d 个评分单元，失效评分 %d 条）"
                    % (decision_info["decision"],
                       decision_info["required_cells"] - decision_info["covered_cells"],
                       decision_info["invalidated_evaluations"]),
                    409,
                )

            criteria = json.loads(batch["criteria"])
            expected_criteria = {c["name"] for c in criteria}
            required_bids = conn.execute(
                """SELECT bb.*,b.vendor_id AS vendor_id,b.price AS price
                   FROM batch_bids bb JOIN bids b ON b.id=bb.bid_id
                   WHERE bb.batch_id=? AND bb.required=1 ORDER BY bb.id""",
                (batch["id"],),
            ).fetchall()
            ranking = []
            for entry in required_bids:
                rows = conn.execute(
                    """SELECT criterion,AVG(score) AS score FROM evaluations
                       WHERE batch_id=? AND bid_id=? AND valid=1 GROUP BY criterion""",
                    (batch["id"], entry["bid_id"]),
                ).fetchall()
                scores = {row["criterion"]: row["score"] for row in rows}
                if set(scores) != expected_criteria:  # 刷新后仍缺项则拒绝授标
                    raise DomainError("投标尚未完成全部有效评分: %s" % entry["bid_id"], 409)
                weighted = 0.0
                for criterion in criteria:
                    weighted += scores[criterion["name"]] * criterion["weight"] / 100
                ranking.append({"bid_id": entry["bid_id"], "vendor_id": entry["vendor_id"],
                                "price": entry["price"], "score": round(weighted, 2)})
            if not ranking:
                raise DomainError("没有可授标的有效投标", 409)
            ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
            winner = ranking[0]
            now = utcnow()
            snapshot = {"tender_id": tender_id, "batch_id": batch["id"],
                        "round": batch["batch_no"], "ranking": ranking, "winner": winner,
                        "awarded_by": actor, "awarded_at": now}
            # 并发授标守卫：只有版本匹配的一个事务能提交。
            cur = conn.execute(
                """UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,
                      evaluations_locked=1,version=version+1,updated_at=?
                   WHERE id=? AND version=? AND status IN ('opened','reevaluation')""",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), now,
                 tender_id, expected_version),
            )
            if cur.rowcount != 1:
                raise DomainError("授标冲突：项目状态已被其他事务更新，请刷新后重试", 409)
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            conn.execute("UPDATE evaluation_batches SET status='awarded',awarded_at=? WHERE id=?", (now, batch["id"]))
            self._audit(conn, tender_id, actor, "tender.awarded",
                        {"batch_id": batch["id"], "winner": winner, "ranking": ranking})
            return {"tender": dict(self._tender(conn, tender_id)),
                    "batch": self._batch_view(conn, self._batch(conn, batch["id"])), "award": snapshot}

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = dict(self._tender(conn, tender_id))
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
            batches = [self._batch_view(conn, b) for b in conn.execute(
                "SELECT * FROM evaluation_batches WHERE tender_id=? ORDER BY batch_no", (tender_id,)
            ).fetchall()]
            return {"tender": tender, "bids": bids, "clarifications": clarifications, "batches": batches}

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            tenders = [dict(r) for r in conn.execute(
                """SELECT id,tender_no,title,description,status,deadline,evaluation_round,
                          current_batch_id,award_frozen,version,awarded_bid_id,created_at,updated_at
                   FROM tenders ORDER BY id DESC"""
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            batches = []
            if role in {"procurement", "supervisor", "auditor"}:
                batches = [dict(r) for r in conn.execute(
                    "SELECT id,tender_id,batch_no,reason,status,activated_at,completed_at,awarded_at,cancelled_at FROM evaluation_batches ORDER BY id DESC LIMIT 200"
                ).fetchall()]
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
        return {"tenders": tenders, "bids": bids, "complaints": complaints, "batches": batches,
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
            elif path == "/api/tenders/recover":
                result = self.service.recover_tender(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/conflicts/retract":
                result = self.service.retract_conflict(actor, role, **data)
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
