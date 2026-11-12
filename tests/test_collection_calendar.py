import unittest
from datetime import date

from energyflow.collection.localtime import (
    business_day_span,
    explain_ambiguous,
    explain_nonexistent,
    load_zone,
    parse_clock,
    resolve_wall,
)


class LocalTimeTests(unittest.TestCase):
    def setUp(self):
        self.ny = load_zone("America/New_York")
        self.berlin = load_zone("Europe/Berlin")

    def test_clock_parsing(self):
        self.assertEqual(parse_clock("02:30"), 9000)
        self.assertEqual(parse_clock("00:00:00"), 0)
        with self.assertRaises(Exception):
            parse_clock("24:00")

    def test_spring_forward_gap_classified_and_explained(self):
        # 纽约 2026-03-08 02:00->03:00，02:30 不存在
        resolved = resolve_wall(date(2026, 3, 8), 2 * 3600 + 1800, self.ny)
        self.assertEqual(resolved["state"], "gap")
        gap_start, gap_end = resolved["gap"]
        self.assertEqual(gap_end - gap_start, 3600)
        skip = explain_nonexistent(resolved, "skip")
        self.assertEqual(skip["action"], "skip")
        self.assertEqual(skip["instants"], [])
        shift = explain_nonexistent(resolved, "shift-forward")
        self.assertEqual(shift["action"], "shift-forward")
        self.assertEqual(shift["instants"], [gap_end])

    def test_fall_back_ambiguous_classified_and_explained(self):
        # 纽约 2026-11-01 01:30 出现两次（-04:00 与 -05:00）
        resolved = resolve_wall(date(2026, 11, 1), 1 * 3600 + 1800, self.ny)
        self.assertEqual(resolved["state"], "ambiguous")
        early, late = resolved["instants"]
        self.assertEqual(late - early, 3600)
        self.assertEqual(explain_ambiguous(resolved, "first")["instants"], [early])
        self.assertEqual(explain_ambiguous(resolved, "second")["instants"], [late])
        both = explain_ambiguous(resolved, "both")
        self.assertEqual(both["instants"], [early, late])
        self.assertEqual(both["dropped"], [])
        first = explain_ambiguous(resolved, "first")
        self.assertEqual(first["dropped"], [late])

    def test_unique_wall_time(self):
        resolved = resolve_wall(date(2026, 6, 1), 12 * 3600, self.ny)
        self.assertEqual(resolved["state"], "unique")
        self.assertEqual(len(resolved["instants"]), 1)

    def test_business_day_spans_are_utc_and_cover_nominal_day(self):
        # 营业日界限 06:00 落在切换日之外时，跨度恒为 24 小时
        for day in (date(2026, 3, 8), date(2026, 11, 1)):
            span = business_day_span(day, 6 * 3600, self.ny)
            self.assertEqual(span["seconds"], 86400)
            self.assertLess(span["start"], span["end"])

    def test_boundary_on_gap_is_recorded(self):
        # 柏林 2026-03-29 02:00->03:00；营业日界限 02:30 落在缺口内
        span = business_day_span(date(2026, 3, 29), 2 * 3600 + 1800, self.berlin)
        codes = [note["detail"]["code"] for note in span["notes"]]
        self.assertIn("boundary_inside_spring_gap", codes)
        # 起点解析到缺口起点，UTC 区间仍然确定且左闭右开
        self.assertLess(span["start"], span["end"])
        self.assertEqual(span["notes"][0]["at"], span["start"])

    def test_ambiguous_boundary_records_first_used(self):
        # 纽约营业日界限 01:00 在 2026-11-01 重复
        span = business_day_span(date(2026, 11, 1), 3600, self.ny)
        codes = [note["detail"]["code"] for note in span["notes"]]
        self.assertIn("boundary_ambiguous_first_used", codes)
        self.assertEqual(span["seconds"], 25 * 3600)

    def test_unknown_zone_rejected(self):
        with self.assertRaises(Exception):
            load_zone("Not/AZone")


if __name__ == "__main__":
    unittest.main()
