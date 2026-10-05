"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
JUDICIAL_ORDER_ROLES = {'case_officer', 'supervisor', 'admin'}
CUSTODY_REVIEW_ROLES = {'case_officer', 'supervisor', 'admin'}
STAY_TYPES = {'stay', 'update', 'revoke'}
STAY_EFFECTIVE_TYPES = {'stay', 'update'}
REVIEW_DECISIONS = {'continue_detention', 'release', 'bond'}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        all_roles.update(JUDICIAL_ORDER_ROLES)
        all_roles.update(CUSTODY_REVIEW_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_judicial_order(self, role: str) -> bool:
        return role in JUDICIAL_ORDER_ROLES

    def role_can_custody_review(self, role: str) -> bool:
        return role in CUSTODY_REVIEW_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        p["detention_start_day"] = optional_integer(p, "detention_start_day", None, 0)
        p["initial_custody_review_days"] = optional_integer(p, "initial_custody_review_days", None, 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
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

    @staticmethod
    def initial_custody_review(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        detention_start = payload.get("detention_start_day")
        if detention_start is None:
            return None
        review_days = payload.get("initial_custody_review_days") or 30
        return {
            "sequence": 1,
            "status": "pending",
            "basis_type": "initial",
            "basis_order_number": "",
            "scheduled_due": int(detention_start) + int(review_days),
            "review_days": int(review_days),
        }

    def validate_judicial_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        issued_day = integer(data, "issued_day", 0)
        order = {
            "order_number": text(data, "order_number"),
            "order_type": choice(data, "order_type", list(STAY_TYPES)),
            "issued_day": issued_day,
            "received_day": optional_integer(data, "received_day", issued_day, 0),
            "effective_day": optional_integer(data, "effective_day", None, 0),
            "resume_day": optional_integer(data, "resume_day", None, 0),
            "review_days": optional_integer(data, "review_days", None, 1),
        }
        if order["effective_day"] is None:
            order["effective_day"] = order["issued_day"]
        if order["resume_day"] is not None and order["resume_day"] < order["effective_day"]:
            raise ValidationError("恢复日期不能早于命令生效日期")
        return order

    @staticmethod
    def _merge_intervals(segments: List[Tuple[int, Optional[int]]]) -> List[Tuple[int, Optional[int]]]:
        finite = sorted((start, end) for start, end in segments if end is not None)
        merged: List[Tuple[int, Optional[int]]] = []
        for start, end in finite:
            if end < start:
                continue
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        open_starts = [start for start, end in segments if end is None]
        if open_starts:
            start = min(open_starts)
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], None)
            else:
                merged.append((start, None))
        return merged

    def build_stay_intervals(self, orders: List[Dict[str, Any]]) -> List[Tuple[int, Optional[int]]]:
        segments: List[Tuple[int, Optional[int]]] = []
        active_start: Optional[int] = None
        active_end: Optional[int] = None
        for order in sorted(orders, key=lambda item: (int(item["issued_day"]), int(item["id"]))):
            order_type = order["order_type"]
            effective_day = int(order.get("effective_day") if order.get("effective_day") is not None else order["issued_day"])
            if order_type in STAY_EFFECTIVE_TYPES:
                if active_start is not None:
                    boundary = effective_day - 1
                    if active_end is None or boundary <= active_end:
                        end = boundary if active_end is None else min(active_end, boundary)
                        if end >= active_start:
                            segments.append((active_start, end))
                    elif active_end is not None:
                        segments.append((active_start, active_end))
                active_start = effective_day
                active_end = int(order["resume_day"]) - 1 if order.get("resume_day") is not None else None
            elif order_type == "revoke":
                resume_day = int(order["resume_day"]) if order.get("resume_day") is not None else effective_day
                if active_start is not None:
                    end = resume_day - 1
                    if end >= active_start:
                        if active_end is not None:
                            end = min(end, active_end)
                        if end >= active_start:
                            segments.append((active_start, end))
                active_start = None
                active_end = None
        if active_start is not None:
            segments.append((active_start, active_end))
        return self._merge_intervals(segments)

    @staticmethod
    def _active_on(intervals: List[Tuple[int, Optional[int]]], day: int) -> bool:
        for start, end in intervals:
            if start <= day and (end is None or day <= end):
                return True
        return False

    @staticmethod
    def _latest_order(orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not orders:
            return None
        return sorted(orders, key=lambda item: (int(item["issued_day"]), int(item["id"])))[-1]

    def plan_judicial_order(
        self,
        record: Dict[str, Any],
        order: Dict[str, Any],
        accepted_orders: List[Dict[str, Any]],
        reviews: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        latest = self._latest_order(accepted_orders)
        before = self.build_stay_intervals(accepted_orders)
        accepted = False
        discrepancy_type = None
        after = before

        if latest is not None and int(order["issued_day"]) <= int(latest["issued_day"]):
            discrepancy_type = "stale_order"
        elif order["order_type"] == "update" and not accepted_orders:
            discrepancy_type = "stale_order"
        elif order["order_type"] == "revoke" and not self._active_on(before, int(order["effective_day"]) - 1):
            discrepancy_type = "stale_order"
        else:
            accepted = True
            after = self.build_stay_intervals(accepted_orders + [dict(order, id=max([int(item["id"]) for item in accepted_orders] + [0]) + 1)])

        new_review = None
        if accepted:
            review_days = order.get("review_days")
            if review_days is None:
                latest_review = self._latest_review(reviews)
                if latest_review is not None and latest_review.get("review_days") is not None:
                    review_days = int(latest_review["review_days"])
                elif record["payload"].get("initial_custody_review_days") is not None:
                    review_days = int(record["payload"]["initial_custody_review_days"])
                elif record["payload"].get("detention_start_day") is not None or reviews:
                    review_days = 30
            if review_days is not None:
                new_review = {
                    "status": "pending",
                    "basis_type": order["order_type"],
                    "basis_order_number": order["order_number"],
                    "scheduled_due": int(order["effective_day"]) + int(review_days),
                    "review_days": int(review_days),
                }

        return {
            "accepted": accepted,
            "discrepancy_type": discrepancy_type,
            "order": order,
            "intervals_before": before,
            "intervals_after": after,
            "superseded_review_ids": [int(item["id"]) for item in reviews if item["status"] == "pending"] if accepted else [],
            "new_review": new_review,
        }

    @staticmethod
    def _latest_review(reviews: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not reviews:
            return None
        return sorted(reviews, key=lambda item: int(item["sequence"]))[-1]

    def office_deadline_basis(self, payload: Dict[str, Any], intervals: List[Tuple[int, Optional[int]]], current_order: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        original_due = int(payload["deadline_day"])
        paused_days = 0
        final_adjustment = True
        for start, end in intervals:
            if end is None:
                final_adjustment = False
                continue
            clipped_start = max(0, int(start))
            clipped_end = min(original_due, int(end))
            if clipped_start <= clipped_end:
                paused_days += clipped_end - clipped_start + 1
        resumed = bool(intervals) and all(end is not None for start, end in intervals)
        resume_day = None
        if current_order is not None:
            if current_order.get("resume_day") is not None:
                resume_day = current_order["resume_day"]
            elif current_order.get("order_type") == "revoke":
                resume_day = current_order.get("effective_day")
        return {
            "original_due_day": original_due,
            "paused_days": paused_days,
            "adjusted_due_day": original_due + paused_days if final_adjustment else None,
            "resume_day": resume_day,
            "stay_status": "stayed" if intervals and not resumed else "resumed" if resumed else "none",
        }

    def build_case_basis(self, record: Dict[str, Any], orders: List[Dict[str, Any]], reviews: List[Dict[str, Any]], discrepancies: List[Dict[str, Any]]) -> Dict[str, Any]:
        intervals = self.build_stay_intervals(orders)
        current_order = self._latest_order(orders)
        office = self.office_deadline_basis(record["payload"], intervals, current_order)
        result = dict(record)
        result["judicial_basis"] = {
            "stay_status": office["stay_status"],
            "current_order_number": None if current_order is None else current_order["order_number"],
            "current_order_type": None if current_order is None else current_order["order_type"],
            "current_effective_day": None if current_order is None else current_order["effective_day"],
            "resume_day": office["resume_day"],
            "stay_intervals": [{"start_day": start, "end_day": end} for start, end in intervals],
            "office_original_due_day": office["original_due_day"],
            "office_paused_days": office["paused_days"],
            "office_adjusted_due_day": office["adjusted_due_day"],
        }
        result["judicial_orders"] = orders
        result["judicial_discrepancies"] = discrepancies
        result["custody_reviews"] = sorted(reviews, key=lambda item: int(item["sequence"]))
        return result

    def validate_custody_review_decision(self, review: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if review["status"] != "pending":
            raise Conflict("只能决定尚未完成的羁押复核")
        decision = choice(data, "decision", list(REVIEW_DECISIONS))
        reason = text(data, "decision_reason")
        decided_day = optional_integer(data, "decided_day", int(review["scheduled_due"]), 0)
        review_days = optional_integer(data, "next_review_days", int(review.get("review_days") or 30), 1)
        follow_up = None
        if decision == "continue_detention":
            follow_up = {
                "status": "pending",
                "basis_type": "follow_up",
                "basis_order_number": review.get("basis_order_number", ""),
                "scheduled_due": decided_day + review_days,
                "review_days": review_days,
            }
        return {"decision": decision, "decision_reason": reason, "decided_day": decided_day, "follow_up": follow_up}
