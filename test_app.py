import tempfile
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001",
            min_age=18, lab_validity_days=14,
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def _eligible_screening(self, user, external_id, age=55, days_ago=2, result="normal"):
        """登记一条当前可判定为合格的筛选记录。"""
        lab = (date.today() - timedelta(days=days_ago)).isoformat()
        return self.store.create_screening(
            user, self.trial["id"], external_id,
            {"consent_date": lab, "age": age, "lab_date": lab, "lab_result": result},
        )

    def _enroll(self, user, screening, factors=None):
        return self.store.enroll(user, self.trial["id"], screening["id"], factors or {"risk": "low"})

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self._enroll("site1", self._eligible_screening("site1", f"S001-{i:03d}"))
            for i in range(1, 5)
        ]
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
        screening = self._eligible_screening("site1", "S001-001", days_ago=1)
        first = self._enroll("site1", screening, {"risk": "high"})
        again = self._enroll("site1", screening, {"risk": "high"})
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

    def test_screening_config_required(self):
        for kwargs in ({"min_age": None, "lab_validity_days": 14},
                       {"min_age": 18, "lab_validity_days": 0},
                       {"min_age": -1, "lab_validity_days": 14}):
            with self.assertRaises(BusinessError):
                self.store.create_trial(
                    "coord", f"配置校验-{kwargs}", "v1", ["A", "B"], ["risk"], 2, "seed-config-001",
                    **kwargs,
                )

    # ---- 入组前筛选门禁 ----

    def test_pending_and_failed_screening_cannot_get_allocation(self):
        # 化验未回报：待复核，入组被挡，且不占用随机号
        pending = self.store.create_screening(
            "site1", self.trial["id"], "P-PENDING",
            {"consent_date": date.today().isoformat(), "age": 60},
        )
        self.assertEqual(pending["status"], "pending")
        self.assertTrue({r["code"] for r in pending["reasons"]} >= {"lab_pending"})
        with self.assertRaises(BusinessError) as ctx:
            self._enroll("site1", pending)
        self.assertEqual(ctx.exception.code, "screening_not_eligible")
        self.assertEqual({r["code"] for r in ctx.exception.details["reasons"]}, {"lab_pending"})
        # 年龄不足且化验异常：不合格
        old_lab = (date.today() - timedelta(days=1)).isoformat()
        failed = self.store.create_screening(
            "site1", self.trial["id"], "P-FAILED",
            {"consent_date": old_lab, "age": 12, "lab_date": old_lab, "lab_result": "abnormal"},
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual({r["code"] for r in failed["reasons"]},
                         {"age_below_minimum", "lab_abnormal"})
        with self.assertRaises(BusinessError) as ctx:
            self._enroll("site1", failed)
        self.assertEqual(ctx.exception.code, "screening_not_eligible")
        # 缺知情同意：不合格
        no_consent = self.store.create_screening(
            "site1", self.trial["id"], "P-NOCONSENT",
            {"age": 60, "lab_date": old_lab, "lab_result": "normal"},
        )
        self.assertEqual(no_consent["status"], "failed")
        self.assertIn("consent_missing", [r["code"] for r in no_consent["reasons"]])
        with self.assertRaises(BusinessError) as ctx:
            self._enroll("site1", no_consent)
        self.assertEqual(ctx.exception.code, "screening_not_eligible")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM allocations WHERE used_by IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 0)

    def test_lab_result_update_rejudges_and_then_enrolls(self):
        screening = self.store.create_screening(
            "site1", self.trial["id"], "P-UPDATE",
            {"consent_date": date.today().isoformat(), "age": 40},
        )
        self.assertEqual(screening["status"], "pending")
        lab = (date.today() - timedelta(days=3)).isoformat()
        updated = self.store.update_screening(
            "site1", screening["id"], {"lab_date": lab, "lab_result": "normal"}
        )
        self.assertEqual(updated["status"], "eligible")
        self.assertTrue(updated["can_enroll"])
        enrolled = self._enroll("site1", updated)
        self.assertTrue(enrolled["allocation_code"])
        got = self.store.get_screening("site1", screening["id"])
        self.assertEqual(got["status"], "enrolled")

    def test_expired_lab_blocks_and_fresh_lab_restores_eligibility(self):
        stale = (date.today() - timedelta(days=30)).isoformat()
        screening = self.store.create_screening(
            "site1", self.trial["id"], "P-EXPIRED",
            {"consent_date": stale, "age": 40, "lab_date": stale, "lab_result": "normal"},
        )
        self.assertEqual(screening["status"], "pending")
        self.assertIn("lab_expired", [r["code"] for r in screening["reasons"]])
        with self.assertRaises(BusinessError) as ctx:
            self._enroll("site1", screening)
        self.assertEqual(ctx.exception.code, "screening_not_eligible")
        # 登记新化验后重新判定为合格
        fresh = (date.today() - timedelta(days=1)).isoformat()
        restored = self.store.update_screening("site1", screening["id"], {"lab_date": fresh, "lab_result": "normal"})
        self.assertEqual(restored["status"], "eligible")
        self._enroll("site1", restored)

    def test_failed_then_rescreen_keeps_history(self):
        old_lab = (date.today() - timedelta(days=1)).isoformat()
        first = self.store.create_screening(
            "site1", self.trial["id"], "P-RESCREEN",
            {"consent_date": old_lab, "age": 50, "lab_date": old_lab, "lab_result": "abnormal"},
        )
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["attempt"], 1)
        # 待复核/合格不能重筛
        pending = self.store.create_screening(
            "site1", self.trial["id"], "P-PENDING2",
            {"consent_date": old_lab, "age": 50},
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.rescreen_screening("site1", pending["id"])
        self.assertEqual(ctx.exception.code, "rescreen_not_allowed")
        second = self.store.rescreen_screening("site1", first["id"])
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(first["id"], second["id"])
        # 旧一轮筛败记录保留且冻结，新一轮待化验
        frozen = self.store.get_screening("site1", first["id"])
        self.assertEqual(frozen["status"], "failed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_screening("site1", first["id"], {"age": 51})
        self.assertEqual(ctx.exception.code, "screening_locked")
        self.assertEqual(second["status"], "pending")
        # 完成新化验后入组；只有新一轮能入组
        lab = (date.today() - timedelta(days=1)).isoformat()
        second = self.store.update_screening("site1", second["id"], {"lab_date": lab, "lab_result": "normal"})
        self.assertEqual(second["status"], "eligible")
        self._enroll("site1", second)
        # 已入组后不能再重筛
        with self.assertRaises(BusinessError) as ctx:
            self.store.rescreen_screening("site1", first["id"])
        self.assertEqual(ctx.exception.code, "screening_locked")
        # 历史完整：同一受试者两条筛选记录
        items = self.store.list_screenings("site1", self.trial["id"])
        rows = [x for x in items if x["external_id"] == "P-RESCREEN"]
        self.assertEqual([(x["attempt"], x["status"]) for x in rows], [(1, "failed"), (2, "enrolled")])

    def test_screening_duplicate_guard(self):
        screening = self.store.create_screening(
            "site1", self.trial["id"], "P-DUP",
            {"consent_date": date.today().isoformat(), "age": 50},
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_screening("site1", self.trial["id"], "P-DUP", {"age": 50})
        self.assertEqual(ctx.exception.code, "screening_exists")
        self.assertEqual(ctx.exception.details["latest_screening_id"], screening["id"])

    def test_screening_site_isolation(self):
        screening = self._eligible_screening("site1", "S001-ISO")
        # 其他中心看不到、改不了、也不能凭它入组
        self.assertEqual(self.store.list_screenings("site2", self.trial["id"]), [])
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_screening("site2", screening["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_screening("site2", screening["id"], {"age": 60})
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site2", self.trial["id"], screening["id"], {"risk": "low"})
        self.assertEqual(ctx.exception.code, "site_isolation")
        # 协调员/监查员可跨中心查看但不能登记/入组
        items = self.store.list_screenings("coord", self.trial["id"])
        self.assertEqual(len(items), 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_screening("coord", self.trial["id"], "X", {"age": 50})
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_only_site_roles_create_screening(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_screening("coord", self.trial["id"], "X", {"age": 50})
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_eligibility_rechecked_at_enroll_time(self):
        # 登记时合格，但入组前化验已过期：必须重新判定并挡住，不发随机号
        screening = self._eligible_screening("site1", "P-LATER", days_ago=1)
        stale = (date.today() - timedelta(days=60)).isoformat()
        with self.store.connect() as conn:
            conn.execute("UPDATE screening_records SET lab_date=? WHERE id=?", (stale, screening["id"]))
        with self.assertRaises(BusinessError) as ctx:
            self._enroll("site1", screening)
        self.assertEqual(ctx.exception.code, "screening_not_eligible")
        self.assertIn("lab_expired", [r["code"] for r in ctx.exception.details["reasons"]])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM allocations WHERE used_by IS NOT NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
