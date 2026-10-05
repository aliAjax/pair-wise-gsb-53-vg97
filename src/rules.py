"""移民案件期限与材料管理领域规则与状态转换。"""
import copy
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, NotFound, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}, 'register_order': {'case_officer', 'supervisor'}, 'decide_review': {'case_officer', 'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}
SIDE_ACTIONS = {'register_order', 'decide_review'}
DEFAULT_CUSTODY_REVIEW_DAYS = 30
ORDER_KINDS = ['issue', 'update', 'revoke']
REVIEW_DECISIONS = ['custody_continued', 'released']


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        if p.get("custody_review_days") is None:
            p["custody_review_days"] = DEFAULT_CUSTODY_REVIEW_DAYS
        else:
            integer(p, "custody_review_days", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["custody_review_days"] = int(p["custody_review_days"])
        p["base_office_deadline_day"] = p["deadline_day"]
        p["base_custody_review_due_day"] = int(p["received_day"]) + p["custody_review_days"]
        p["stayed_days"] = 0
        p["office_deadline_day"] = p["base_office_deadline_day"]
        p["custody_review_due_day"] = p["base_custody_review_due_day"]
        p["stay"] = {"active": False, "current_order_no": None, "current_seq": -1, "interval": None, "banked_days": 0}
        p["reviews"] = [self._new_review([], p["custody_review_due_day"], "initial", None, 0)]
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        if action in SIDE_ACTIONS:
            if record["state"] == "closed":
                raise Conflict("案件已归档，不允许执行%s" % action)
            return record["state"]
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 司法暂缓命令与羁押复核 ----

    @staticmethod
    def _stay_state(p: Dict[str, Any]) -> Dict[str, Any]:
        stay = p.get("stay") or {}
        stay.setdefault("active", False)
        stay.setdefault("current_order_no", None)
        stay.setdefault("current_seq", -1)
        stay.setdefault("interval", None)
        stay.setdefault("banked_days", 0)
        p.setdefault("base_office_deadline_day", int(p.get("deadline_day", 0)))
        p.setdefault("base_custody_review_due_day", int(p.get("deadline_day", 0)))
        p.setdefault("reviews", [])
        p["stay"] = stay
        return stay

    @staticmethod
    def _new_review(reviews: List[Dict[str, Any]], due_day: int, reason: str, order: Optional[Dict[str, Any]], stayed_days: int) -> Dict[str, Any]:
        next_id = max([int(r.get("review_id", 0)) for r in reviews], default=0) + 1
        basis = {"order_no": order["order_no"] if order else None, "seq": order["seq"] if order else -1, "stayed_days": stayed_days, "custody_review_due_day": due_day}
        return {"review_id": next_id, "kind": "custody", "status": "pending", "due_day": due_day, "reason": reason, "created_by_order": basis["order_no"], "basis": basis}

    def validate_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data or {})
        order = {
            "order_no": text(data, "order_no"),
            "seq": integer(data, "seq", 0),
            "kind": choice(data, "kind", ORDER_KINDS),
            "issued_day": integer(data, "issued_day", 0),
            "start_day": None,
            "end_day": None,
            "note": optional_text(data, "note"),
        }
        if order["kind"] in ("issue", "update"):
            order["start_day"] = integer(data, "start_day", 0)
            order["end_day"] = integer(data, "end_day", 0)
            if order["end_day"] <= order["start_day"]:
                raise ValidationError("暂缓结束日必须晚于开始日")
        elif data.get("end_day") is not None:
            order["end_day"] = integer(data, "end_day", 0)
        return order

    def apply_order(self, record: Dict[str, Any], order: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        """套用法院命令：较新命令改写当前停表区间，迟到的旧命令只留作差异。"""
        p = copy.deepcopy(record["payload"])
        stay = self._stay_state(p)
        order_row = dict(order)
        if stay["current_seq"] >= 0 and order["seq"] <= stay["current_seq"]:
            order_row["status"] = "divergent"
            summary = "命令%s序号%s不高于当前命令%s(序号%s)，留作差异，停表区间不变" % (order["order_no"], order["seq"], stay["current_order_no"], stay["current_seq"])
            return p, order_row, summary
        if order["kind"] in ("issue", "update"):
            stay["interval"] = {"start_day": order["start_day"], "end_day": order["end_day"]}
            stay["active"] = True
        else:
            if stay["interval"]:
                end_day = order["end_day"] if order["end_day"] is not None else order["issued_day"]
                if end_day < stay["interval"]["start_day"]:
                    raise ValidationError("撤销生效日早于暂缓开始日")
                stay["banked_days"] += end_day - stay["interval"]["start_day"]
            stay["interval"] = None
            stay["active"] = False
        stay["current_seq"] = order["seq"]
        stay["current_order_no"] = order["order_no"]
        order_row["status"] = "applied"
        self._recalculate_clock(p, stay)
        superseded = self._refresh_reviews(p, order)
        summary = "命令%s已生效，累计停表%s天，办案期限第%s天，羁押复核期限第%s天" % (order["order_no"], p["stayed_days"], p["office_deadline_day"], p["custody_review_due_day"])
        if superseded:
            summary += "，%s条未决复核已失效重算" % superseded
        return p, order_row, summary

    def _recalculate_clock(self, p: Dict[str, Any], stay: Dict[str, Any]) -> None:
        stayed = int(stay["banked_days"])
        if stay["interval"]:
            stayed += int(stay["interval"]["end_day"]) - int(stay["interval"]["start_day"])
        p["stayed_days"] = stayed
        p["office_deadline_day"] = int(p["base_office_deadline_day"]) + stayed
        p["custody_review_due_day"] = int(p["base_custody_review_due_day"]) + stayed
        p["deadline_day"] = p["office_deadline_day"]
        p["days_remaining"] = p["deadline_day"] - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0

    def _refresh_reviews(self, p: Dict[str, Any], order: Dict[str, Any]) -> int:
        """命令生效后：未决复核失效重算，已决复核保留原依据并追加新复核。"""
        reviews = p["reviews"]
        superseded = 0
        for review in reviews:
            if review["status"] == "pending":
                review["status"] = "superseded"
                review["superseded_by_order"] = order["order_no"]
                superseded += 1
        reason = "recalculated" if superseded else "follow_up"
        reviews.append(self._new_review(reviews, p["custody_review_due_day"], reason, order, p["stayed_days"]))
        return superseded

    def apply_review_decision(self, record: Dict[str, Any], data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        data = dict(data or {})
        review_id = integer(data, "review_id", 1)
        decision = choice(data, "decision", REVIEW_DECISIONS)
        decided_day = integer(data, "decided_day", 0)
        p = copy.deepcopy(record["payload"])
        self._stay_state(p)
        for review in p["reviews"]:
            if int(review.get("review_id", 0)) == review_id:
                if review["status"] != "pending":
                    raise Conflict("复核#%s已失效或已决定" % review_id)
                review["status"] = "decided"
                review["decision"] = decision
                review["decided_day"] = decided_day
                review["decision_note"] = optional_text(data, "decision_note")
                return p, "复核#%s已决定：%s，原依据保留" % (review_id, decision)
        raise NotFound("复核#%s不存在" % review_id)
