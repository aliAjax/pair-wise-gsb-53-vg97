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
        created = self.repository.create(
            reference,
            self.rules.INITIAL_STATE,
            prepared,
            actor.user_id,
            self.rules.initial_custody_review(prepared),
        )
        return self._judicial_basis(self.repository.get_judicial_case(created["id"]))

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._judicial_basis(self.repository.get_judicial_case(record_id))

    def _judicial_basis(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        return self.rules.build_case_basis(
            bundle["record"],
            bundle["orders"],
            bundle["reviews"],
            bundle["discrepancies"],
        )

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        saved = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        return self._judicial_basis({
            "record": saved,
            "orders": self.repository.judicial_orders(record_id),
            "reviews": self.repository.custody_reviews(record_id),
            "discrepancies": self.repository.judicial_discrepancies(record_id),
        })

    def save_judicial_order(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_judicial_order(actor.role):
            raise PermissionDenied("角色无权登记司法暂缓命令")
        order = self.rules.validate_judicial_order(data or {})
        result = self.repository.save_judicial_order(
            record_id,
            int(expected_version),
            order,
            actor.user_id,
            lambda current: self.rules.plan_judicial_order(current["record"], order, current["orders"], current["reviews"]),
        )
        status = result.pop("status")
        warning = result.pop("warning", None)
        response = self._judicial_basis(result)
        response["save_result"] = {"status": status, "warning": warning}
        return response

    def decide_custody_review(self, actor: Actor, record_id: int, review_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_custody_review(actor.role):
            raise PermissionDenied("角色无权处理羁押复核")
        review = self.repository.custody_review(record_id, review_id)
        decision = self.rules.validate_custody_review_decision(review, data or {})
        result = self.repository.decide_custody_review(record_id, review_id, int(expected_version), decision, actor.user_id)
        return self._judicial_basis(result)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
