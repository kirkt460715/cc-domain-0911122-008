import tempfile
import unittest
from pathlib import Path

from energyflow.collection.runtime import Clock, ScriptedClient
from energyflow.service import EnergyService

NY = "America/New_York"


def full_source(start, end, limit):
    n = (end - start) // 3600
    rows = [
        {"id": "r%d" % tick, "at": tick, "value": 100, "kind": "normal"}
        for tick in range(start, end, 3600)
    ]
    complete = n <= limit
    return {
        "rows": rows[:limit],
        "complete": complete,
        "coverage": {"start": start, "end": end} if complete else None,
    }


class CollectionEngineTests(unittest.TestCase):
    def setUp(self):
        self.service = EnergyService()
        self.addCleanup(self.service.close)
        self.service.catalog.register("m", "NY plant")
        self.service.catalog.install("seg", "m", 0, 0, max_gap=7200)
        self.clock = Clock(1773000000)
        self.service.scheduler.clock = self.clock
        self.service.scheduler.engine.clock = self.clock
        self.client = ScriptedClient(full_source)
        self.service.scheduler.client = self.client
        self.service.scheduler.engine.client = self.client
        self.scheduler = self.service.scheduler

    def plan(self, **overrides):
        definition = {
            "zone": NY,
            "business_start": "06:00",
            "trigger": "02:30",
            "ambiguous": "both",
            "nonexistent": "shift-forward",
            "segment": "seg",
            "endpoint": "http://loopback",
            "catchup": "all",
        }
        definition.update(overrides)
        return self.scheduler.create_plan("p", "m", definition)

    def test_materialize_preview_and_collect_full_window(self):
        self.plan()
        preview = self.scheduler.preview("p", "2026-03-08", "2026-03-09")
        self.assertEqual(len(preview["days"]), 2)
        self.scheduler.materialize("p", "2026-03-08", "2026-03-09")
        self.clock.set(1773140000)
        report = self.scheduler.tick()
        self.assertEqual(report["ran"], 2)
        windows = {w["business_date"]: w for w in self.scheduler.windows("p")}
        self.assertTrue(all(w["status"] == "collected" for w in windows.values()))
        coverage = self.scheduler.coverage("p")
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["totals"]["missing_seconds"], 0)

    def test_spring_gap_skip_abandons_with_reason(self):
        self.plan(nonexistent="skip")
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        window = self.scheduler.windows("p")[0]
        self.assertEqual(window["status"], "abandoned")
        self.assertIsNone(window["trigger_at"])
        codes = [n["detail"]["code"] for n in self.scheduler.notes("p", "2026-03-08")]
        self.assertIn("nonexistent_local_time_skipped", codes)
        self.assertIn("window_abandoned_trigger_skipped", codes)

    def test_fall_ambiguous_both_materializes_two_windows(self):
        self.plan(trigger="01:30", ambiguous="both")
        self.scheduler.materialize("p", "2026-11-01", "2026-11-01")
        windows = self.scheduler.windows("p")
        self.assertEqual(len(windows), 2)
        trigger_ats = sorted(w["trigger_at"] for w in windows)
        self.assertEqual(trigger_ats[1] - trigger_ats[0], 3600)
        # 两个窗口覆盖同一个营业日 UTC 区间，靠运行所有权避免重复请求
        self.clock.set(1793620000)
        first = self.scheduler.tick(max_runs=1)
        self.assertEqual(first["ran"], 1)
        second = self.scheduler.tick(max_runs=1)
        # 第二个触发运行时，区间已被第一个窗口采完，不应再请求来源
        requests_before = len(self.client.requests)
        self.scheduler.tick()
        self.assertEqual(len(self.client.requests), requests_before)
        statuses = {w["id"]: w["status"] for w in self.scheduler.windows("p")}
        self.assertIn("collected", set(statuses.values()))

    def test_maintenance_clip_is_never_requested_nor_counted_missing(self):
        self.plan(maintenance=[
            {"start": "2026-03-09T10:00", "end": "2026-03-09T12:00", "reason": "pause"}
        ])
        self.scheduler.materialize("p", "2026-03-09", "2026-03-09")
        self.clock.set(1773140000)
        self.scheduler.tick()
        coverage = self.scheduler.coverage("p", "2026-03-09", "2026-03-09")
        row = coverage["windows"][0]
        self.assertEqual(row["status"], "collected")
        self.assertEqual(row["missing_seconds"], 0)
        self.assertEqual(coverage["totals"]["excluded_seconds"], 7200)
        for request in self.client.requests:
            # 没有任何请求区间与维护段（UTC 14:00-16:00）相交
            self.assertFalse(request["start"] < 1773072000 < request["end"])

    def test_full_day_maintenance_abandons_window(self):
        self.plan(maintenance=[{"start": "2026-03-09", "end": "2026-03-09"}])
        self.scheduler.materialize("p", "2026-03-09", "2026-03-09")
        self.assertEqual(self.scheduler.windows("p")[0]["status"], "abandoned")

    def test_holiday_is_skipped_not_collected(self):
        self.plan(holidays=["2026-03-09"])
        self.scheduler.materialize("p", "2026-03-09", "2026-03-09")
        window = self.scheduler.windows("p")[0]
        self.assertEqual(window["status"], "skipped")
        self.clock.set(1773140000)
        self.scheduler.tick()
        self.assertEqual(self.client.requests, [])

    def test_plan_revision_does_not_reinterpret_existing_windows(self):
        self.plan(nonexistent="skip")
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        old = self.scheduler.window("p.2026-03-08")
        self.assertEqual((old["revision"], old["status"]), (1, "abandoned"))
        self.plan(nonexistent="shift-forward", trigger="03:30")
        self.assertEqual(len(self.scheduler.plan_versions("p")), 2)
        # 重新物化同一日：旧窗口原样保留，不产生新窗口
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        again = self.scheduler.window("p.2026-03-08")
        self.assertEqual(again["revision"], 1)
        self.assertEqual(again["status"], "abandoned")
        # 新日期按新版本解释
        self.scheduler.materialize("p", "2026-03-11", "2026-03-11")
        self.assertEqual(self.scheduler.window("p.2026-03-11")["revision"], 2)

    def test_budget_exhaustion_resumes_next_tick(self):
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-12")
        self.clock.set(1773400000)
        first = self.scheduler.tick(max_runs=2, max_readings=10)
        self.assertTrue(first["budget_exhausted"])
        self.assertLess(len([w for w in self.scheduler.windows("p")
                             if w["status"] == "collected"]), 5)
        # 放宽预算后继续，最终全部采完
        self.scheduler.tick(max_runs=20, max_readings=100)
        self.assertTrue(self.scheduler.coverage("p")["complete"])

    def test_empty_rows_without_evidence_keep_window_open(self):
        def no_evidence(start, end, limit):
            return {"rows": [], "complete": False, "coverage": None}

        self.service.scheduler.client = ScriptedClient(no_evidence)
        self.service.scheduler.engine.client = self.service.scheduler.client
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        self.clock.set(1773140000)
        self.scheduler.tick()
        window = self.scheduler.windows("p")[0]
        self.assertIn(window["status"], ("ready", "partial", "failed"))
        self.assertNotEqual(window["status"], "collected")
        self.assertFalse(self.scheduler.coverage("p")["complete"])
        # 接回正常来源后可补齐
        self.service.scheduler.client = self.client
        self.service.scheduler.engine.client = self.client
        self.scheduler.tick()
        self.assertEqual(self.scheduler.windows("p")[0]["status"], "collected")

    def test_latest_catchup_abandons_older_days_explicitly(self):
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-12")
        self.clock.set(1773400000)
        result = self.scheduler.backfill(
            "p", "2026-03-08", "2026-03-12", mode="latest", max_runs=20
        )
        self.assertEqual(result["mode"], "latest")
        windows = {w["business_date"]: w["status"] for w in self.scheduler.windows("p")}
        self.assertEqual(windows["2026-03-08"], "abandoned")
        self.assertEqual(windows["2026-03-12"], "collected")
        notes = [n["detail"]["code"] for n in self.scheduler.notes("p", "2026-03-08")]
        self.assertIn("latest_catchup_older_day_abandoned", notes)

    def test_all_catchup_collects_every_materialized_day(self):
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-11")
        self.clock.set(1773400000)
        self.scheduler.backfill("p", "2026-03-08", "2026-03-11", mode="all", max_runs=20)
        self.assertTrue(self.scheduler.coverage("p")["complete"])

    def test_operator_abandons_gap_with_evidence_note(self):
        def hole(start, end, limit):
            # 来源在 3 个小时处有真实缺口：覆盖证据只给到缺口起点
            covered_end = start + 3 * 3600
            rows = [
                {"id": "r%d" % tick, "at": tick, "value": 5}
                for tick in range(start, covered_end, 3600)
            ]
            n = (covered_end - start) // 3600
            complete = n <= limit and covered_end >= end
            return {
                "rows": rows[:limit],
                "complete": complete,
                "coverage": {"start": start, "end": end} if complete else None,
            }

        self.service.scheduler.client = ScriptedClient(hole)
        self.service.scheduler.engine.client = self.service.scheduler.client
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        self.clock.set(1773140000)
        self.scheduler.tick()
        window_id = "p.2026-03-08"
        coverage_row = self.scheduler.coverage("p")["windows"][0]
        # 来源没有给出 complete/coverage：即便有部分读数，也不拥有任何区间，
        # 未采范围必须如实显示，不能伪装成部分完成。
        self.assertEqual(coverage_row["missing_seconds"], 86400)
        self.assertEqual(coverage_row["covered_seconds"], 0)
        self.scheduler.abandon_gap(window_id, "meter confirmed unrecoverable")
        self.assertEqual(self.scheduler.window(window_id)["status"], "abandoned")
        notes = [n for n in self.scheduler.notes("p", "2026-03-08")
                 if n["kind"] == "abandoned_gap"]
        self.assertTrue(notes)
        report = self.scheduler.coverage("p")
        self.assertEqual(report["totals"]["abandoned_seconds"], 86400)
        self.assertFalse(report["complete"])

    def test_manual_backfill_overlap_claims_ownership_once(self):
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        self.clock.set(1773140000)
        self.scheduler.tick()
        requests_after_schedule = len(self.client.requests)
        # 再次补采同一窗口：覆盖已存在，不会重复请求来源
        self.scheduler.backfill("p", "2026-03-08", "2026-03-08", mode="all")
        self.assertEqual(len(self.client.requests), requests_after_schedule)

    def test_persistence_across_restart(self):
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-09")
        self.clock.set(1773140000)
        self.scheduler.tick(max_runs=1)
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "restart.db"
            self.service.store.backup(db)
            with EnergyService(db) as restarted:
                windows = restarted.scheduler.windows("p")
                self.assertTrue(any(w["attempts"] == 1 for w in windows))
                self.assertTrue(restarted.store.integrity()["verified"])
                self.assertTrue(restarted.store.verify_audit()["verified"])

    def test_coverage_totals_union_overlapping_dst_days(self):
        # 营业日界限 01:00：秋季切换日为 25 小时，与相邻日连续排列成 49 小时
        self.plan(business_start="01:00", trigger="00:30")
        self.scheduler.materialize("p", "2026-10-31", "2026-11-01")
        self.clock.set(1793620000)
        self.scheduler.tick(max_runs=20, max_readings=100)
        coverage = self.scheduler.coverage("p", "2026-10-31", "2026-11-01")
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["totals"]["requested_seconds"], 49 * 3600)
        self.assertEqual(coverage["totals"]["covered_seconds"], 49 * 3600)

    def test_ambiguous_both_windows_share_coverage_without_double_count(self):
        # both 策略为同一营业日物化两个窗口，它们覆盖的 UTC 区间完全相同；
        # 汇总并集只能计一次 24 小时。
        self.plan(trigger="01:30", ambiguous="both")
        self.scheduler.materialize("p", "2026-11-01", "2026-11-01")
        self.assertEqual(len(self.scheduler.windows("p")), 2)
        self.clock.set(1793620000)
        self.scheduler.tick(max_runs=20, max_readings=100)
        coverage = self.scheduler.coverage("p", "2026-11-01", "2026-11-01")
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["totals"]["requested_seconds"], 2 * 86400)
        self.assertEqual(coverage["totals"]["covered_seconds"], 86400)
        # 来源只被请求一次（区间所有权在两个窗口间协调）
        self.assertEqual(len(self.client.requests), 1)

    def test_stale_lease_is_reclaimed_and_window_retries(self):
        self.plan()
        self.scheduler.materialize("p", "2026-03-08", "2026-03-08")
        window_id = "p.2026-03-08"
        self.clock.set(1773140000)
        # 手工制造一个租约已过期的运行中尝试（模拟进程崩溃）
        with self.service.store.atomic() as con:
            con.execute(
                "UPDATE collection_windows SET status='running',attempts=1 WHERE id=?",
                (window_id,),
            )
            con.execute(
                "INSERT INTO collection_runs "
                "(id,window_id,kind,owner,request_start,request_end,started_at,"
                "finished_at,status,readings,covered,reason,payload,digest,lease_expires_at) "
                "VALUES(?,?,?,?,?,?,?,NULL,'running',0,'[]',NULL,?,?,?)",
                (
                    "run.stale.1", window_id, "scheduled", "tick",
                    1772964000, 1773050400, self.clock() - 1000,
                    '{"id":"run.stale.1"}',
                    __import__("energyflow.values", fromlist=["fingerprint"]).fingerprint(
                        {"id": "run.stale.1"}
                    ),
                    self.clock() - 1,
                ),
            )
        report = self.scheduler.tick()
        self.assertGreaterEqual(report["ran"], 1)
        stale = next(r for r in self.scheduler.runs(window_id) if r["id"] == "run.stale.1")
        self.assertEqual(stale["status"], "failed")
        self.assertEqual(self.scheduler.window(window_id)["status"], "collected")

    def test_dispatch_exposes_collect_actions(self):
        result = self.service.dispatch({
            "action": "collect.create_plan",
            "arguments": {
                "plan_id": "q",
                "meter": "m",
                "definition": {
                    "zone": NY,
                    "business_start": "06:00",
                    "trigger": "02:30",
                    "segment": "seg",
                },
            },
        })
        self.assertEqual(result["id"], "q")
        listed = self.service.dispatch({"action": "collect.list_plans", "arguments": {}})
        self.assertIn({"id": "q", "revision": 1}, listed)


if __name__ == "__main__":
    unittest.main()
