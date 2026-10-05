import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CASE_DATA = {
    'applicant_id': 'A-7700',
    'case_type': 'asylum',
    'received_day': 0,
    'deadline_days': 30,
    'response_day': 0,
    'representation_active': True,
    'required_documents': ['passport'],
    'detention_start_day': 5,
    'initial_custody_review_days': 7,
}
OFFICER = Actor('officer', 'case_officer')


class JudicialStayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))
        self.record = self.service.create(Actor('creator', 'intake_officer'), 'IMM-JS-1', CASE_DATA)
        self.record_id = self.record['id']

    def tearDown(self):
        self.temp.cleanup()

    def save_order(self, version, **order):
        return self.service.save_judicial_order(OFFICER, self.record_id, version, order)

    def test_stay_duplicate_and_late_order_basis(self):
        detail = self.service.get_record(OFFICER, self.record_id)
        self.assertEqual(detail['judicial_basis']['stay_status'], 'none')
        self.assertEqual([(item['status'], item['scheduled_due']) for item in detail['custody_reviews']], [('pending', 12)])

        stayed = self.save_order(
            1,
            order_number='STAY-1',
            order_type='stay',
            issued_day=10,
            received_day=10,
            effective_day=10,
            review_days=5,
        )
        self.assertEqual(stayed['version'], 2)
        self.assertEqual(stayed['judicial_basis']['stay_status'], 'stayed')
        self.assertEqual(stayed['judicial_basis']['stay_intervals'], [{'start_day': 10, 'end_day': None}])
        self.assertIsNone(stayed['judicial_basis']['office_adjusted_due_day'])
        self.assertEqual(
            [(item['sequence'], item['status'], item['scheduled_due'], item['basis_type'], item['basis_order_number']) for item in stayed['custody_reviews']],
            [(1, 'superseded', 12, 'initial', ''), (2, 'pending', 15, 'stay', 'STAY-1')],
        )

        retry = self.save_order(
            1,
            order_number='STAY-1',
            order_type='stay',
            issued_day=10,
            received_day=10,
            effective_day=10,
            review_days=5,
        )
        self.assertEqual(retry['save_result']['status'], 'duplicate')
        self.assertEqual(retry['version'], 2)
        self.assertEqual(len(retry['judicial_orders']), 1)
        self.assertEqual(len(retry['custody_reviews']), 2)

        changed = self.save_order(
            1,
            order_number='STAY-1',
            order_type='update',
            issued_day=11,
            received_day=11,
            effective_day=11,
            resume_day=20,
        )
        self.assertEqual(changed['save_result']['status'], 'duplicate')
        self.assertEqual(changed['judicial_discrepancies'][0]['discrepancy_type'], 'duplicate_conflict')
        self.assertEqual(changed['judicial_orders'][0]['order_type'], 'stay')

        stale = self.save_order(2, order_number='STAY-OLD', order_type='stay', issued_day=8, received_day=11)
        self.assertEqual(stale['save_result']['status'], 'discrepancy')
        self.assertEqual(stale['version'], 2)
        self.assertTrue(any(item['discrepancy_type'] == 'stale_order' for item in stale['judicial_discrepancies']))
        self.assertEqual(stale['judicial_basis']['stay_intervals'], [{'start_day': 10, 'end_day': None}])

    def test_newer_order_recalculates_periods_and_decisions_keep_basis(self):
        self.save_order(1, order_number='STAY-1', order_type='stay', issued_day=10, review_days=5)
        resumed = self.save_order(2, order_number='UPDATE-1', order_type='update', issued_day=12, received_day=12, effective_day=12, resume_day=15, review_days=5)
        self.assertEqual(resumed['version'], 3)
        self.assertEqual(resumed['judicial_basis']['stay_intervals'], [{'start_day': 10, 'end_day': 14}])
        self.assertEqual(resumed['judicial_basis']['office_paused_days'], 5)
        self.assertEqual(resumed['judicial_basis']['office_adjusted_due_day'], 35)
        self.assertEqual([item['status'] for item in resumed['custody_reviews']], ['superseded', 'superseded', 'pending'])

        pending = next(item for item in resumed['custody_reviews'] if item['status'] == 'pending')
        decided = self.service.decide_custody_review(
            OFFICER,
            self.record_id,
            pending['id'],
            3,
            {'decision': 'continue_detention', 'decision_reason': '继续羁押', 'decided_day': 16, 'next_review_days': 4},
        )
        self.assertEqual(decided['version'], 4)
        completed = next(item for item in decided['custody_reviews'] if item['sequence'] == 3)
        self.assertEqual(completed['status'], 'continue_detention')
        self.assertEqual(completed['basis_order_number'], 'UPDATE-1')
        self.assertEqual(decided['custody_reviews'][-1]['status'], 'pending')
        self.assertEqual(decided['custody_reviews'][-1]['scheduled_due'], 20)

        self.save_order(4, order_number='STAY-2', order_type='stay', issued_day=18, received_day=18, effective_day=18, review_days=3)
        revoked = self.save_order(5, order_number='REVOKE-1', order_type='revoke', issued_day=20, received_day=20, effective_day=20, resume_day=20, review_days=4)
        self.assertEqual(revoked['version'], 6)
        self.assertEqual(revoked['judicial_basis']['current_order_number'], 'REVOKE-1')
        self.assertEqual(
            revoked['judicial_basis']['stay_intervals'],
            [{'start_day': 10, 'end_day': 14}, {'start_day': 18, 'end_day': 19}],
        )
        self.assertEqual(revoked['judicial_basis']['office_paused_days'], 7)
        self.assertEqual(revoked['judicial_basis']['office_adjusted_due_day'], 37)
        completed_after = next(item for item in revoked['custody_reviews'] if item['sequence'] == 3)
        self.assertEqual(completed_after['status'], 'continue_detention')
        self.assertEqual(completed_after['basis_order_number'], 'UPDATE-1')
        self.assertEqual([item['status'] for item in revoked['custody_reviews']], [
            'superseded', 'superseded', 'continue_detention', 'superseded', 'superseded', 'pending'
        ])
        self.assertEqual(revoked['custody_reviews'][-1]['scheduled_due'], 24)

    def test_concurrent_newer_orders_only_first_advances(self):
        self.save_order(1, order_number='STAY-1', order_type='stay', issued_day=10, review_days=5)
        with self.assertRaises(Conflict):
            self.save_order(1, order_number='UPDATE-1', order_type='update', issued_day=12, effective_day=12, resume_day=15)
        won = self.save_order(2, order_number='UPDATE-1', order_type='update', issued_day=12, received_day=12, effective_day=12, resume_day=15)
        self.assertEqual(won['version'], 3)
        self.assertEqual(won['judicial_basis']['office_adjusted_due_day'], 35)
