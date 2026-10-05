"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action == "register_order":
            return self._register_order(actor, record_id, int(expected_version), data or {})
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "decide_review":
            new_state = record["state"]
            new_payload, summary = self.rules.apply_review_decision(record, data or {})
        else:
            new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def _register_order(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        order = self.rules.validate_order(data)
        if self.repository.get_order(record_id, order["order_no"]) is not None:
            # 同一命令号只收一次：保存失败后的重试在此幂等返回，期限不再延长
            self.audit.note(record_id, actor.user_id, "order_duplicate", {"order_no": order["order_no"], "summary": "重复命令已忽略，期限不再延长"})
            return record
        self.rules.require_transition(record, "register_order")
        new_payload, order_row, summary = self.rules.apply_order(record, order)
        record, applied = self.repository.mutate_with_order(
            record_id=record_id,
            expected_version=expected_version,
            state=record["state"],
            payload=new_payload,
            actor_id=actor.user_id,
            action="register_order",
            details={"summary": summary, "order": order_row},
            order=order_row,
        )
        if not applied:
            self.audit.note(record_id, actor.user_id, "order_duplicate", {"order_no": order["order_no"], "summary": "重复命令已忽略，期限不再延长"})
        return record

    def basis(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """同一份可续作依据：两套期限、停表状态、命令对账记录与复核队列。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        p = record["payload"]
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "state": record["state"],
            "version": record["version"],
            "office_deadline_day": p.get("office_deadline_day", p.get("deadline_day")),
            "custody_review_due_day": p.get("custody_review_due_day"),
            "stayed_days": p.get("stayed_days", 0),
            "stay": p.get("stay"),
            "orders": self.repository.list_orders(record_id),
            "reviews": p.get("reviews", []),
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
