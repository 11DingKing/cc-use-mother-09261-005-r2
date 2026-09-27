"""SQLite 状态仓储。

设计要点：
- 采用固定应用时间 ``effective_at``（默认 UTC 现在），所有业务时间戳显式传入，
  跨日期边界重查同一批数据时结果可重现，不依赖墙上时钟。
- cases 保存每个观察的*当前*行（状态、版本、学校、观察日、签发冻结快照）；
  transitions / evidence 两张明细表只追加，构成完整历史。
- 写操作在单个事务里完成，状态机推进与历史写入原子提交。
"""

import json
import sqlite3
from datetime import datetime, timezone

from .errors import ConflictError, NotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    school          TEXT NOT NULL,
    observed_on     TEXT NOT NULL,
    actor           TEXT NOT NULL,
    state           TEXT NOT NULL,
    version         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    frozen_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    seq         INTEGER NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    UNIQUE(case_id, seq)
);
CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL REFERENCES cases(id),
    kind         TEXT NOT NULL,
    ref          TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL,
    added_at     TEXT NOT NULL,
    superseded_at TEXT,
    supersede_note TEXT NOT NULL DEFAULT '',
    UNIQUE(case_id, id)
);
CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    request     TEXT NOT NULL,
    response    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_query
    ON cases(school, observed_on, state);
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    """统一以带时区偏移的 ISO-8601 落库。"""
    if dt.tzinfo is None:
        raise ValueError("datetime 必须带时区信息")
    return dt.isoformat(timespec="seconds")


class SQLiteStore:
    def __init__(self, path=":memory:", now=utcnow):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._now = now

    # ---- 基础工具 -------------------------------------------------

    def clock(self):
        return self._now()

    def get_case_row(self, conn, case_id):
        row = conn.execute(
            "SELECT * FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(case_id)
        return row

    @staticmethod
    def _case_dict(row):
        d = dict(row)
        d["frozen_snapshot"] = (
            json.loads(d["frozen_snapshot"]) if d["frozen_snapshot"] else None
        )
        return d

    # ---- 幂等键 ---------------------------------------------------

    def find_idempotent(self, key, request_fingerprint):
        """返回 (case_id, response)；键冲突时抛 ConflictError。"""
        if not key:
            return None
        row = self.db.execute(
            "SELECT case_id, request, response FROM idempotency WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request"] != request_fingerprint:
            raise ConflictError("idempotency key 已用于不同请求")
        return row["case_id"], json.loads(row["response"])

    def save_idempotent(self, key, case_id, request_fingerprint, response):
        if not key:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO idempotency(key, case_id, request, response)"
            " VALUES(?,?,?,?)",
            (key, case_id, request_fingerprint, json.dumps(response, ensure_ascii=False)),
        )

    # ---- 读 -------------------------------------------------------

    def get_case(self, case_id):
        return self._case_dict(self.get_case_row(self.db, case_id))

    def list_evidence(self, case_id):
        rows = self.db.execute(
            "SELECT id, kind, ref, note, actor, added_at, superseded_at,"
            " supersede_note FROM evidence WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_transitions(self, case_id):
        rows = self.db.execute(
            "SELECT seq, from_state, to_state, reason, actor, effective_at"
            " FROM transitions WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def query_cases(self, school=None, observed_on=None, state=None,
                    date_from=None, date_to=None):
        """按学校 / 观察日 / 当前状态检索。

        observed_on 为单日精确匹配；date_from/date_to 为闭区间。
        只查 cases 当前行，已被新状态取代的旧状态绝不会混进结果。
        """
        sql = "SELECT * FROM cases WHERE 1=1"
        args = []
        if school:
            sql += " AND school = ?"
            args.append(school)
        if observed_on:
            sql += " AND observed_on = ?"
            args.append(observed_on)
        if date_from:
            sql += " AND observed_on >= ?"
            args.append(date_from)
        if date_to:
            sql += " AND observed_on <= ?"
            args.append(date_to)
        if state:
            sql += " AND state = ?"
            args.append(state)
        sql += " ORDER BY observed_on, id"
        rows = self.db.execute(sql, args).fetchall()
        return [self._case_dict(r) for r in rows]

    # ---- 写（各方法自行管理事务） ---------------------------------

    def insert_case(self, case, first_transition, at):
        with self.db:
            exists = self.db.execute(
                "SELECT 1 FROM cases WHERE id = ?", (case["id"],)
            ).fetchone()
            if exists:
                raise ConflictError("观察记录已存在: %s" % case["id"])
            self.db.execute(
                "INSERT INTO cases(id, school, observed_on, actor, state, version,"
                " created_at, updated_at, frozen_snapshot)"
                " VALUES(:id,:school,:observed_on,:actor,:state,:version,"
                ":created_at,:updated_at,NULL)",
                {**case, "created_at": iso(at), "updated_at": iso(at)},
            )
            self.db.execute(
                "INSERT INTO transitions(case_id, seq, from_state, to_state,"
                " reason, actor, effective_at) VALUES(?,?,?,?,?,?,?)",
                (first_transition["case_id"], 1,
                 first_transition["from_state"], first_transition["to_state"],
                 first_transition["reason"], first_transition["actor"],
                 iso(at)),
            )

    def insert_evidence(self, case_id, ev, at):
        with self.db:
            row = self.get_case_row(self.db, case_id)
            if row["state"] == "issued":
                raise ConflictError("记录已签发，证据关系冻结，不可再补充")
            cur = self.db.execute(
                "INSERT INTO evidence(case_id, kind, ref, note, actor, added_at)"
                " VALUES(?,?,?,?,?,?)",
                (case_id, ev["kind"], ev["ref"], ev["note"], ev["actor"], iso(at)),
            )
            return cur.lastrowid

    def supersede_evidence(self, case_id, evidence_id, actor, note, at):
        """软撤回一条证据：只追加撤回标记，不删除原行。"""
        with self.db:
            row = self.get_case_row(self.db, case_id)
            if row["state"] == "issued":
                raise ConflictError("记录已签发，证据关系冻结，不可撤回证据")
            ev = self.db.execute(
                "SELECT id, superseded_at FROM evidence"
                " WHERE case_id = ? AND id = ?",
                (case_id, evidence_id),
            ).fetchone()
            if ev is None:
                raise NotFoundError("证据 %s" % evidence_id)
            if ev["superseded_at"] is not None:
                raise ConflictError("证据已处于撤回状态")
            self.db.execute(
                "UPDATE evidence SET superseded_at = ?, supersede_note = ?"
                " WHERE id = ?",
                (iso(at), note or "", evidence_id),
            )
            return evidence_id

    def apply_transition(self, case_id, to_state, reason, actor, expected_version, at,
                         frozen_snapshot=None):
        """乐观并发推进：expected_version 与当前版本不符则拒绝。"""
        with self.db:
            row = self.get_case_row(self.db, case_id)
            if row["version"] != expected_version:
                raise ConflictError(
                    "版本冲突：当前 v%d，提交基于 v%d"
                    % (row["version"], expected_version)
                )
            next_seq = expected_version + 1  # 创建时 seq=1，之后每次推进与版本对齐
            self.db.execute(
                "UPDATE cases SET state = ?, version = version + 1,"
                " updated_at = ?, frozen_snapshot = ?"
                " WHERE id = ? AND version = ?",
                (to_state, iso(at),
                 json.dumps(frozen_snapshot, ensure_ascii=False)
                 if frozen_snapshot is not None else row["frozen_snapshot"],
                 case_id, expected_version),
            )
            self.db.execute(
                "INSERT INTO transitions(case_id, seq, from_state, to_state,"
                " reason, actor, effective_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, next_seq, row["state"], to_state, reason, actor, iso(at)),
            )
            return row["version"] + 1

    def close(self):
        self.db.close()
