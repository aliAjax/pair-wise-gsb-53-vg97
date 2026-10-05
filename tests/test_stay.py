import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'custody_review_days': 20, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
CREATOR = Actor("creator", "intake_officer")
OFFICER_A = Actor("officer-a", "case_officer")
OFFICER_B = Actor("officer-b", "case_officer")


def order(order_no, seq, kind='issue', start_day=None, end_day=None, issued_day=100):
    data = {'order_no': order_no, 'seq': seq, 'kind': kind, 'issued_day': issued_day}
    if start_day is not None:
        data['start_day'] = start_day
    if end_day is not None:
        data['end_day'] = end_day
    return data


class StayOrderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self.service.create(CREATOR, "IMM-STAY-1", CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def basis(self):
        return self.service.basis(OFFICER_A, self.record["id"])

    def register(self, actor, version, data):
        return self.service.act(actor, self.record["id"], version, "register_order", data)

    def test_initial_basis_has_two_clocks_and_pending_review(self):
        basis = self.basis()
        self.assertEqual(basis["office_deadline_day"], 130)
        self.assertEqual(basis["custody_review_due_day"], 120)
        self.assertEqual(basis["stayed_days"], 0)
        self.assertEqual(basis["orders"], [])
        self.assertEqual(len(basis["reviews"]), 1)
        review = basis["reviews"][0]
        self.assertEqual(review["status"], "pending")
        self.assertEqual(review["due_day"], 120)
        self.assertEqual(review["reason"], "initial")

    def test_order_extends_both_deadlines_and_recalculates_review(self):
        record = self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        self.assertEqual(record["payload"]["stayed_days"], 10)
        basis = self.basis()
        self.assertEqual(basis["office_deadline_day"], 140)
        self.assertEqual(basis["custody_review_due_day"], 130)
        self.assertTrue(basis["stay"]["active"])
        self.assertEqual(basis["stay"]["current_order_no"], "ORD-1")
        statuses = [(r["review_id"], r["status"], r["due_day"]) for r in basis["reviews"]]
        self.assertEqual(statuses, [(1, "superseded", 120), (2, "pending", 130)])
        self.assertEqual(basis["reviews"][1]["basis"]["order_no"], "ORD-1")

    def test_same_order_no_accepted_once_and_retry_does_not_extend(self):
        first = self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        # 保存失败后按同一命令号重试：即使版本已过期也幂等返回，日期不再延长
        retry = self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        self.assertEqual(retry["payload"]["stayed_days"], 10)
        self.assertEqual(retry["version"], first["version"])
        basis = self.basis()
        self.assertEqual(len(basis["orders"]), 1)
        self.assertEqual(basis["office_deadline_day"], 140)
        actions = [e["action"] for e in self.service.timeline(OFFICER_A, self.record["id"])]
        self.assertIn("order_duplicate", actions)

    def test_late_old_order_kept_as_divergence(self):
        self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        self.register(OFFICER_A, 2, order("ORD-2", 2, kind='update', start_day=105, end_day=120))
        late = self.register(OFFICER_A, 3, order("ORD-0", 1, start_day=100, end_day=150))
        self.assertEqual(late["payload"]["stayed_days"], 15)
        orders = {o["order_no"]: o for o in self.basis()["orders"]}
        self.assertEqual(orders["ORD-0"]["status"], "divergent")
        self.assertEqual(orders["ORD-2"]["status"], "applied")
        self.assertEqual(self.basis()["stay"]["current_order_no"], "ORD-2")

    def test_newer_order_rewrites_interval_instead_of_stacking(self):
        self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        updated = self.register(OFFICER_A, 2, order("ORD-2", 2, kind='update', start_day=105, end_day=125))
        # 改写当前停表区间：累计停表为新区间长度20天，而非10+20
        self.assertEqual(updated["payload"]["stayed_days"], 20)
        basis = self.basis()
        self.assertEqual(basis["office_deadline_day"], 150)
        self.assertEqual(basis["custody_review_due_day"], 140)

    def test_revoke_banks_days_and_later_stay_accumulates(self):
        self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        revoked = self.register(OFFICER_A, 2, order("ORD-2", 2, kind='revoke', end_day=118, issued_day=118))
        self.assertEqual(revoked["payload"]["stayed_days"], 13)
        self.assertFalse(revoked["payload"]["stay"]["active"])
        again = self.register(OFFICER_A, 3, order("ORD-3", 3, start_day=130, end_day=140))
        self.assertEqual(again["payload"]["stayed_days"], 23)
        self.assertEqual(again["payload"]["office_deadline_day"], 153)

    def test_decided_review_keeps_basis_and_follow_up_is_appended(self):
        record = self.service.act(OFFICER_A, self.record["id"], 1, "decide_review", {"review_id": 1, "decision": "custody_continued", "decided_day": 118})
        decided = record["payload"]["reviews"][0]
        self.assertEqual(decided["status"], "decided")
        original_basis = dict(decided["basis"])
        record = self.register(OFFICER_A, record["version"], order("ORD-1", 1, start_day=120, end_day=130))
        reviews = record["payload"]["reviews"]
        self.assertEqual(reviews[0]["status"], "decided")
        self.assertEqual(reviews[0]["basis"], original_basis)
        self.assertEqual(reviews[1]["status"], "pending")
        self.assertEqual(reviews[1]["reason"], "follow_up")
        self.assertEqual(reviews[1]["due_day"], 130)

    def test_superseded_review_cannot_be_decided(self):
        self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        with self.assertRaises(Conflict):
            self.service.act(OFFICER_A, self.record["id"], 2, "decide_review", {"review_id": 1, "decision": "released", "decided_day": 116})
        decided = self.service.act(OFFICER_A, self.record["id"], 2, "decide_review", {"review_id": 2, "decision": "released", "decided_day": 116})
        self.assertEqual(decided["payload"]["reviews"][1]["status"], "decided")

    def test_concurrent_officers_first_commit_wins_and_retry_is_idempotent(self):
        # 经办甲先落库
        first = self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        self.assertEqual(first["payload"]["stayed_days"], 10)
        # 经办乙持同一版本并发办理，收到版本冲突，填写内容保留在己方表单
        with self.assertRaises(Conflict):
            self.register(OFFICER_B, 1, order("ORD-2", 2, kind='update', start_day=105, end_day=120))
        # 乙刷新版本后原样重试成功
        fresh = self.service.get_record(OFFICER_B, self.record["id"])
        second = self.register(OFFICER_B, fresh["version"], order("ORD-2", 2, kind='update', start_day=105, end_day=120))
        self.assertEqual(second["payload"]["stayed_days"], 15)
        # 甲以为保存失败，按同一命令号重试：幂等返回，日期不会再次延长
        retry = self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=105, end_day=115))
        self.assertEqual(retry["payload"]["stayed_days"], 15)
        orders = self.basis()["orders"]
        self.assertEqual(len([o for o in orders if o["order_no"] == "ORD-1"]), 1)
        self.assertEqual(len(orders), 2)

    def test_order_validation(self):
        with self.assertRaises(ValidationError):
            self.register(OFFICER_A, 1, order("ORD-1", 1, start_day=115, end_day=105))
        with self.assertRaises(ValidationError):
            self.register(OFFICER_A, 1, {'order_no': 'ORD-1', 'seq': 1, 'kind': 'issue', 'issued_day': 100})
        with self.assertRaises(ValidationError):
            self.register(OFFICER_A, 1, {'order_no': '', 'seq': 1, 'kind': 'issue', 'issued_day': 100, 'start_day': 1, 'end_day': 2})
