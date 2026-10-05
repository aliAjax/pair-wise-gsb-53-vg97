"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS judicial_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    order_number TEXT NOT NULL,
                    order_type TEXT NOT NULL CHECK(order_type IN ('stay','update','revoke')),
                    issued_day INTEGER NOT NULL,
                    received_day INTEGER NOT NULL,
                    effective_day INTEGER NOT NULL,
                    resume_day INTEGER,
                    review_days INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, order_number)
                );
                CREATE TABLE IF NOT EXISTS judicial_discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    order_number TEXT NOT NULL,
                    discrepancy_type TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS custody_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','superseded','continue_detention','release','bond')),
                    basis_type TEXT NOT NULL,
                    basis_order_number TEXT NOT NULL DEFAULT '',
                    scheduled_due INTEGER NOT NULL,
                    review_days INTEGER,
                    decision_reason TEXT,
                    decided_day INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_by TEXT,
                    updated_at TEXT,
                    UNIQUE(record_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_orders_record ON judicial_orders(record_id, issued_day, id);
                CREATE INDEX IF NOT EXISTS idx_discrepancies_record ON judicial_discrepancies(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reviews_record ON custody_reviews(record_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _order_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("id", "record_id", "issued_day", "received_day", "effective_day", "resume_day", "review_days"):
            if item.get(key) is not None:
                item[key] = int(item[key])
        return item

    @staticmethod
    def _review_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("id", "record_id", "sequence", "scheduled_due", "review_days", "decided_day"):
            if item.get(key) is not None:
                item[key] = int(item[key])
        return item

    @staticmethod
    def _discrepancy_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["id"] = int(item["id"])
        item["record_id"] = int(item["record_id"])
        item["details"] = json.loads(item["details"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, initial_review: Dict[str, Any] = None) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dumps(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                if initial_review is not None:
                    connection.execute(
                        """
                        INSERT INTO custody_reviews(record_id,sequence,status,basis_type,basis_order_number,scheduled_due,review_days,created_by,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            record_id,
                            int(initial_review["sequence"]),
                            initial_review["status"],
                            initial_review["basis_type"],
                            initial_review.get("basis_order_number", ""),
                            int(initial_review["scheduled_due"]),
                            initial_review.get("review_days"),
                            actor_id,
                            now,
                        ),
                    )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dumps({"state": state, "initial_custody_review": initial_review is not None}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def judicial_orders(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM judicial_orders WHERE record_id=? ORDER BY issued_day,id", (record_id,)).fetchall()
        return [self._order_row(row) for row in rows]

    def judicial_discrepancies(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM judicial_discrepancies WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._discrepancy_row(row) for row in rows]

    def custody_reviews(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM custody_reviews WHERE record_id=? ORDER BY sequence", (record_id,)).fetchall()
        return [self._review_row(row) for row in rows]

    def custody_review(self, record_id: int, review_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM custody_reviews WHERE record_id=? AND id=?", (record_id, review_id)).fetchone()
        if row is None:
            raise NotFound("羁押复核不存在")
        return self._review_row(row)

    def get_judicial_case(self, record_id: int) -> Dict[str, Any]:
        record = self.get(record_id)
        return {
            "record": record,
            "orders": self.judicial_orders(record_id),
            "reviews": self.custody_reviews(record_id),
            "discrepancies": self.judicial_discrepancies(record_id),
        }

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, _dumps(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dumps(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def save_judicial_order(self, record_id: int, expected_version: int, order: Dict[str, Any], actor_id: str, planner) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current_version = int(row["version"])
            existing = connection.execute(
                "SELECT * FROM judicial_orders WHERE record_id=? AND order_number=?",
                (record_id, order["order_number"]),
            ).fetchone()
            if existing is not None:
                same_payload = existing["order_type"] == order["order_type"] and all(
                    int(existing[key] if existing[key] is not None else -1) == int(order[key] if order[key] is not None else -1)
                    for key in ("issued_day", "received_day", "effective_day", "resume_day", "review_days")
                )
                if not same_payload:
                    connection.execute(
                        """
                        INSERT INTO judicial_discrepancies(record_id,order_number,discrepancy_type,details,created_by,created_at)
                        VALUES(?,?,?,?,?,?)
                        """,
                        (record_id, order["order_number"], "duplicate_conflict", _dumps({"received": order, "existing": dict(existing)}), actor_id, now),
                    )
                connection.commit()
                result = self.get_judicial_case(record_id)
                result["status"] = "duplicate"
                result["warning"] = "同一案件同一命令号已登记，未重复停表"
                return result

            order_rows = connection.execute("SELECT * FROM judicial_orders WHERE record_id=? ORDER BY issued_day,id", (record_id,)).fetchall()
            review_rows = connection.execute("SELECT * FROM custody_reviews WHERE record_id=? ORDER BY sequence", (record_id,)).fetchall()
            bundle = {
                "record": self._row(row),
                "orders": [self._order_row(item) for item in order_rows],
                "reviews": [self._review_row(item) for item in review_rows],
            }
            plan = planner(bundle)

            if current_version != int(expected_version) and plan["accepted"]:
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            if not plan["accepted"]:
                connection.execute(
                    """
                    INSERT INTO judicial_discrepancies(record_id,order_number,discrepancy_type,details,created_by,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (record_id, order["order_number"], plan["discrepancy_type"], _dumps({"order": order}), actor_id, now),
                )
                connection.commit()
                result = self.get_judicial_case(record_id)
                result["status"] = "discrepancy"
                result["warning"] = "迟到的旧命令已留作差异，当前停表区间未改写"
                return result

            connection.execute(
                """
                INSERT INTO judicial_orders(record_id,order_number,order_type,issued_day,received_day,effective_day,resume_day,review_days,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    order["order_number"],
                    order["order_type"],
                    int(order["issued_day"]),
                    int(order["received_day"]),
                    int(order["effective_day"]),
                    order.get("resume_day"),
                    order.get("review_days"),
                    actor_id,
                    now,
                ),
            )
            if plan["superseded_review_ids"]:
                connection.execute(
                    "UPDATE custody_reviews SET status='superseded',updated_by=?,updated_at=? WHERE record_id=? AND status='pending'",
                    (actor_id, now, record_id),
                )
            if plan["new_review"] is not None:
                max_sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM custody_reviews WHERE record_id=?",
                    (record_id,),
                ).fetchone()["max_sequence"]
                review = plan["new_review"]
                connection.execute(
                    """
                    INSERT INTO custody_reviews(record_id,sequence,status,basis_type,basis_order_number,scheduled_due,review_days,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record_id,
                        int(max_sequence) + 1,
                        review["status"],
                        review["basis_type"],
                        review.get("basis_order_number", ""),
                        int(review["scheduled_due"]),
                        review.get("review_days"),
                        actor_id,
                        now,
                    ),
                )
            version = current_version + 1
            connection.execute(
                "UPDATE records SET version=?,updated_by=?,updated_at=? WHERE id=?",
                (version, actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "judicial_order_" + order["order_type"], actor_id, version, _dumps({"order": order, "plan": {
                    "superseded_review_ids": plan["superseded_review_ids"],
                    "new_review": plan["new_review"],
                    "intervals_after": plan["intervals_after"],
                }}), now),
            )
            connection.commit()
        result = self.get_judicial_case(record_id)
        result["status"] = "accepted"
        return result

    def decide_custody_review(self, record_id: int, review_id: int, expected_version: int, decision: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            review_row = connection.execute("SELECT * FROM custody_reviews WHERE id=? AND record_id=?", (review_id, record_id)).fetchone()
            if review_row is None:
                connection.rollback()
                raise NotFound("羁押复核不存在")
            if review_row["status"] != "pending":
                connection.rollback()
                raise Conflict("只能决定尚未完成的羁押复核")
            connection.execute(
                """
                UPDATE custody_reviews
                SET status=?,decision_reason=?,decided_day=?,updated_by=?,updated_at=?
                WHERE id=?
                """,
                (decision["decision"], decision["decision_reason"], int(decision["decided_day"]), actor_id, now, review_id),
            )
            if decision.get("follow_up") is not None:
                max_sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM custody_reviews WHERE record_id=?",
                    (record_id,),
                ).fetchone()["max_sequence"]
                follow_up = decision["follow_up"]
                connection.execute(
                    """
                    INSERT INTO custody_reviews(record_id,sequence,status,basis_type,basis_order_number,scheduled_due,review_days,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record_id,
                        int(max_sequence) + 1,
                        follow_up["status"],
                        follow_up["basis_type"],
                        follow_up.get("basis_order_number", ""),
                        int(follow_up["scheduled_due"]),
                        follow_up.get("review_days"),
                        actor_id,
                        now,
                    ),
                )
            version = int(record_row["version"]) + 1
            connection.execute("UPDATE records SET version=?,updated_by=?,updated_at=? WHERE id=?", (version, actor_id, now, record_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "custody_review_decision", actor_id, version, _dumps({"review_id": review_id, "decision": decision}), now),
            )
            connection.commit()
        return self.get_judicial_case(record_id)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), _dumps(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
