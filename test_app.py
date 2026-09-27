import tempfile
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, RandomizationStore

TODAY = date.today().isoformat()


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001", 18, 30
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def screen(self, external_id, site="site1", **kw):
        data = dict(consent_signed=True, consent_date=TODAY, age=30, lab_date=TODAY, lab_result="normal")
        data.update(kw)
        return self.store.submit_screening(site, self.trial["id"], external_id, **data)

    def enroll(self, external_id, site="site1", risk="low"):
        return self.store.enroll(site, self.trial["id"], external_id, {"risk": risk})

    def used_allocations(self):
        with self.store.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM allocations WHERE used_by IS NOT NULL").fetchone()[0]

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = []
        for i in range(1, 5):
            self.screen(f"S001-{i:03d}")
            participants.append(self.enroll(f"S001-{i:03d}"))
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        self.screen("S001-001")
        first = self.enroll("S001-001", risk="high")
        again = self.enroll("S001-001", risk="high")
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")

    def test_screening_judgement(self):
        self.assertEqual(self.screen("S001-100")["status"], "eligible")
        under_age = self.screen("S001-101", age=16)
        self.assertEqual(under_age["status"], "ineligible")
        self.assertIn("最低年龄", under_age["reasons"][0])
        no_consent = self.screen("S001-102", consent_signed=False, consent_date=None)
        self.assertEqual(no_consent["status"], "ineligible")
        self.assertEqual(self.screen("S001-103", lab_result="pending")["status"], "pending_review")
        self.assertEqual(self.screen("S001-104", lab_result="abnormal")["status"], "pending_review")
        old_lab = self.screen("S001-105", lab_date=(date.today() - timedelta(days=45)).isoformat())
        self.assertEqual(old_lab["status"], "pending_review")
        self.assertTrue(old_lab["lab_expired"])

    def test_enrollment_blocked_without_passing_screening(self):
        with self.assertRaises(BusinessError) as ctx:
            self.enroll("S001-200")
        self.assertEqual(ctx.exception.code, "screening_required")
        self.screen("S001-201", age=10)
        with self.assertRaises(BusinessError) as ctx:
            self.enroll("S001-201")
        self.assertEqual(ctx.exception.code, "screening_ineligible")
        self.assertTrue(ctx.exception.detail["reasons"])
        self.screen("S001-202", lab_result="pending")
        with self.assertRaises(BusinessError) as ctx:
            self.enroll("S001-202")
        self.assertEqual(ctx.exception.code, "screening_pending_review")
        self.assertEqual(self.used_allocations(), 0)

    def test_lab_expired_after_screening_blocks_enrollment(self):
        screening = self.screen("S001-300")
        self.assertEqual(screening["status"], "eligible")
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE screenings SET lab_date=? WHERE id=?",
                ((date.today() - timedelta(days=45)).isoformat(), screening["id"]),
            )
        with self.assertRaises(BusinessError) as ctx:
            self.enroll("S001-300")
        self.assertEqual(ctx.exception.code, "screening_lab_expired")
        self.assertEqual(self.used_allocations(), 0)

    def test_rescreening_updates_status_and_keeps_history(self):
        first = self.screen("S001-400", lab_result="pending")
        self.assertEqual(first["status"], "pending_review")
        second = self.screen("S001-400", lab_result="normal")
        self.assertEqual(second["status"], "eligible")
        self.assertEqual(second["attempt"], 2)
        history = self.store.list_screenings("site1", self.trial["id"], external_id="S001-400")
        self.assertEqual([h["attempt"] for h in history], [1, 2])
        self.assertTrue(history[0]["superseded"])
        participant = self.enroll("S001-400")
        self.assertIn("allocation_code", participant)
        with self.assertRaises(BusinessError) as ctx:
            self.screen("S001-400")
        self.assertEqual(ctx.exception.code, "screening_locked")

    def test_screening_site_isolation(self):
        self.screen("S001-500")
        self.assertEqual(self.store.list_screenings("site2", self.trial["id"]), [])
        self.assertEqual(len(self.store.list_screenings("coord", self.trial["id"])), 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site2", self.trial["id"], "S001-500", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "screening_required")

    def test_screening_criteria_validation(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_trial("coord", "另一项研究", "v1", ["A", "B"], ["risk"], 2, "seed-2026-002", -1, 30)
        self.assertEqual(ctx.exception.code, "invalid_min_age")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_trial("coord", "另一项研究", "v1", ["A", "B"], ["risk"], 2, "seed-2026-002", 18, 0)
        self.assertEqual(ctx.exception.code, "invalid_lab_validity")


if __name__ == "__main__":
    unittest.main()
