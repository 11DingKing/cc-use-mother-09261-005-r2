"""版本化业务工作流。

状态机：
    draft ──提交审核──▶ reviewing ──审核通过──▶ issued   （终态，冻结）
      ▲                   │
      └──── 退回补充 ─────┘
    draft / reviewing ──取消──▶ cancelled（终态）

规则：
- 观察先以 draft 草稿保存，证据可在 draft / reviewing 阶段分次补充；
- 只有 reviewing 状态且至少有一条有效证据，才允许签发 issued；
- 签发时把当时状态与有效证据关系写入 frozen_snapshot，之后证据与状态均不可变；
- 每次状态变化都在 transitions 留痕（原因、操作人、生效时间）。
"""

import re
from dataclasses import dataclass, asdict

from .errors import ConflictError, ValidationError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ALLOWED = {
    "draft": {"reviewing", "cancelled"},
    "reviewing": {"issued", "draft", "cancelled"},
    "issued": set(),
    "cancelled": set(),
}

ACTIVE_EVIDENCE_STATES = {"draft", "reviewing"}


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s 不能为空" % field)
    return value.strip()


def _validate_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValidationError("observed_on 必须是 YYYY-MM-DD 日期")
    return value


@dataclass(frozen=True)
class Case:
    id: str
    school: str
    observed_on: str
    actor: str
    state: str
    version: int = 1

    def move(self, state, actor):
        if state not in ALLOWED.get(self.state, set()):
            raise ConflictError("不允许的状态迁移: %s -> %s" % (self.state, state))
        return Case(self.id, self.school, self.observed_on, actor,
                    state, self.version + 1)


class Workflow:
    def __init__(self, store):
        self.store = store

    # ---- 命令 -----------------------------------------------------

    def create(self, id, actor, school=None, observed_on=None,
               reason=None, idempotency_key=None):
        cid = _require_text(id, "id")
        who = _require_text(actor, "actor")
        sch = _require_text(school, "school")
        day = _validate_date(_require_text(observed_on, "observed_on"))
        why = (reason or "观察记录以草稿创建").strip()

        fingerprint = 'create:%s:%s:%s:%s' % (cid, sch, day, who)
        cached = self.store.find_idempotent(idempotency_key, fingerprint)
        if cached is not None:
            _case_id, response = cached
            return response

        case = Case(cid, sch, day, who, "draft")
        at = self.store.clock()
        self.store.insert_case(
            asdict(case),
            {"case_id": cid, "from_state": None, "to_state": "draft",
             "reason": why, "actor": who},
            at,
        )
        response = self.get_case(cid)
        self.store.save_idempotent(idempotency_key, cid, fingerprint, response)
        return response

    def add_evidence(self, case_id, actor, kind, ref, note=""):
        cid = _require_text(case_id, "case_id")
        who = _require_text(actor, "actor")
        ev_kind = _require_text(kind, "kind")
        ev_ref = _require_text(ref, "ref")
        case = self.store.get_case(cid)
        if case["state"] not in ACTIVE_EVIDENCE_STATES:
            raise ConflictError("当前状态 %s 不允许补充证据" % case["state"])
        new_id = self.store.insert_evidence(
            cid,
            {"kind": ev_kind, "ref": ev_ref, "note": (note or "").strip(),
             "actor": who},
            self.store.clock(),
        )
        return self.get_case(cid, evidence_id=new_id)

    def withdraw_evidence(self, case_id, evidence_id, actor, note=""):
        cid = _require_text(case_id, "case_id")
        who = _require_text(actor, "actor")
        case = self.store.get_case(cid)
        if case["state"] not in ACTIVE_EVIDENCE_STATES:
            raise ConflictError("当前状态 %s 不允许撤回证据" % case["state"])
        self.store.supersede_evidence(
            cid, int(evidence_id), who, (note or "").strip(), self.store.clock()
        )
        return self.get_case(cid)

    def move(self, case_id, state, actor, reason=None, expected_version=None):
        cid = _require_text(case_id, "case_id")
        who = _require_text(actor, "actor")
        to = _require_text(state, "state")
        why = _require_text(reason, "reason")

        case = self.store.get_case(cid)
        if to not in ALLOWED.get(case["state"], set()):
            raise ConflictError("不允许的状态迁移: %s -> %s" % (case["state"], to))

        version = (int(expected_version) if expected_version is not None
                   else case["version"])
        frozen = None
        if to == "issued":
            frozen = self._build_issued_snapshot(cid)

        self.store.apply_transition(
            cid, to, why, who, version, self.store.clock(),
            frozen_snapshot=frozen,
        )
        return self.get_case(cid)

    def _build_issued_snapshot(self, case_id):
        evidence = self.store.list_evidence(case_id)
        active = [e for e in evidence if e["superseded_at"] is None]
        if not active:
            raise ConflictError("至少需要一条有效证据才能签发")
        case = self.store.get_case(case_id)
        return {
            "state": "issued",
            "from_version": case["version"],
            "evidence": [
                {"id": e["id"], "kind": e["kind"], "ref": e["ref"],
                 "note": e["note"]}
                for e in active
            ],
            "issued_at": self.store.clock().isoformat(timespec="seconds"),
        }

    # ---- 查询 -----------------------------------------------------

    def get_case(self, case_id, evidence_id=None):
        case = self.store.get_case(case_id)
        evidence = self.store.list_evidence(case_id)
        transitions = self.store.list_transitions(case_id)
        body = {
            "id": case["id"],
            "school": case["school"],
            "observed_on": case["observed_on"],
            "state": case["state"],
            "version": case["version"],
            "created_at": case["created_at"],
            "updated_at": case["updated_at"],
            "evidence": evidence,
            "active_evidence_count": sum(
                1 for e in evidence if e["superseded_at"] is None
            ),
            "transitions": transitions,
            "frozen_snapshot": case["frozen_snapshot"],
        }
        if evidence_id is not None:
            body["added_evidence_id"] = evidence_id
        return body

    def query(self, school=None, observed_on=None, state=None,
              date_from=None, date_to=None):
        if observed_on:
            _validate_date(observed_on)
        if date_from:
            _validate_date(date_from)
        if date_to:
            _validate_date(date_to)
        return self.store.query_cases(
            school=school, observed_on=observed_on, state=state,
            date_from=date_from, date_to=date_to,
        )

    def snapshot(self):
        return self.query()
