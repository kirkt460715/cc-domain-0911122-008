"""端到端剧情演示：两个 DST 切换日、计划改版、停机追赶与明确放弃的缺口。

全程使用注入时钟与本地回环网关，不需要真的等待一个营业日。运行：
    python3 -m energyflow.collection_demo
输出同时写入 var/collection_demo_report.json。
"""

import json
from datetime import datetime
from pathlib import Path

from lab.collection_provider import CollectionProvider
from lab.http import serving

from .collection.localtime import iso_utc
from .collection.runtime import Clock
from .service import EnergyService


def _post(base, path, body):
    import urllib.request

    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def _epoch(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def run(write_file=True):
    report = {"chapters": []}
    # 演示起点：2026-03-07 营业日开始前（纽约时间）
    clock = Clock(_epoch("2026-03-07T11:00:00Z"))
    provider = CollectionProvider(clock=clock)

    with serving(provider) as base, EnergyService() as service:
        service.scheduler.clock = clock
        service.scheduler.engine.clock = clock
        service.catalog.register("ny-plant", "纽约海外工厂电表")
        service.catalog.install("ny-seg", "ny-plant", 0, 0, max_gap=7200)

        # 网关侧真实数据缺口：11-02 营业日内两小时，来源无法恢复
        provider.gaps.append((
            datetime.fromisoformat("2026-11-02T15:00:00+00:00"),
            datetime.fromisoformat("2026-11-02T17:00:00+00:00"),
        ))

        # ---- 第 1 章：建立计划 v1（春季触发 02:30 落在缺口 -> skip；
        #      秋季用独立计划演示 01:30 重复时刻的 both 展开）并预览 DST 日 ----
        service.scheduler.create_plan("ny", "ny-plant", {
            "zone": "America/New_York",
            "business_start": "02:00",
            "trigger": "02:30",
            "ambiguous": "both",
            "nonexistent": "skip",
            "segment": "ny-seg",
            "endpoint": base,
            "holidays": ["2026-11-26"],
            "maintenance": [
                {"start": "2026-11-01T03:00", "end": "2026-11-01T05:00",
                 "reason": "现场手工暂停校准"},
            ],
            "catchup": "all",
        })
        # 同一份日历在 both 策略下的秋季预览（仅用于展示，不落库）
        service.scheduler.create_plan("ny-amb", "ny-plant", {
            "zone": "America/New_York",
            "business_start": "02:00",
            "trigger": "01:30",
            "ambiguous": "both",
            "nonexistent": "shift-forward",
            "segment": "ny-seg",
            "endpoint": base,
        })
        spring_preview = service.scheduler.preview("ny", "2026-03-08", "2026-03-08")
        fall_preview = service.scheduler.preview("ny-amb", "2026-11-01", "2026-11-01")
        report["chapters"].append({
            "chapter": "1_计划与DST预览",
            "spring_day": spring_preview["days"][0],
            "fall_switch_day": fall_preview["days"][0],
        })

        # ---- 第 2 章：物化并跨过春季切换日（不存在时刻 skip -> 放弃留痕） ----
        service.scheduler.materialize("ny", "2026-03-07", "2026-03-09")
        clock.set(_epoch("2026-03-10T12:00:00Z"))
        spring_tick = service.scheduler.tick(max_runs=10, max_readings=100)
        report["chapters"].append({
            "chapter": "2_春季切换_skip",
            "tick": spring_tick,
            "windows": [
                {"business_date": w["business_date"], "status": w["status"],
                 "revision": w["revision"], "trigger_at":
                 iso_utc(w["trigger_at"]) if w["trigger_at"] else None}
                for w in service.scheduler.windows("ny")
            ],
            "notes": service.scheduler.notes("ny", "2026-03-08"),
        })

        # ---- 第 3 章：计划改版 v2（shift-forward + first），旧窗口不被重新解释 ----
        service.scheduler.create_plan("ny", "ny-plant", {
            "zone": "America/New_York",
            "business_start": "02:00",
            "trigger": "02:30",
            "ambiguous": "first",
            "nonexistent": "shift-forward",
            "segment": "ny-seg",
            "endpoint": base,
            "holidays": ["2026-11-26"],
            "maintenance": [
                {"start": "2026-11-01T03:00", "end": "2026-11-01T05:00",
                 "reason": "现场手工暂停校准"},
            ],
            "catchup": "all",
        })
        versions = service.scheduler.plan_versions("ny")
        # 物化秋季切换日及之后的营业日
        service.scheduler.materialize("ny", "2026-10-30", "2026-11-04")
        old_spring = service.scheduler.window("ny.2026-03-08")
        report["chapters"].append({
            "chapter": "3_计划改版_旧窗口处置",
            "versions": versions,
            "old_spring_window": {
                "id": old_spring["id"],
                "status": old_spring["status"],
                "revision": old_spring["revision"],
            },
            "new_windows_revision": sorted({
                w["revision"] for w in service.scheduler.windows("ny")
                if w["business_date"] >= "2026-10-30"
            }),
        })

        # ---- 第 4 章：秋季切换日，重复时刻 first + 维护排除段 ----
        clock.set(_epoch("2026-11-02T12:00:00Z"))
        fall_tick = service.scheduler.tick(max_runs=10, max_readings=100)
        fall_window = service.scheduler.window("ny.2026-11-01")
        fall_coverage = service.scheduler.coverage("ny", "2026-11-01", "2026-11-01")
        report["chapters"].append({
            "chapter": "4_秋季切换_维护排除",
            "tick": fall_tick,
            "window_status": fall_window["status"],
            "coverage": fall_coverage["windows"][0],
            "notes": service.scheduler.notes("ny", "2026-11-01"),
        })

        # ---- 第 5 章：服务停机数天，恢复后逐日追赶 ----
        down_start = "2026-11-03T00:00:00Z"
        down_end = "2026-11-07T00:00:00Z"
        _post(base, "/admin/down", {"start": down_start, "end": down_end})
        service.scheduler.materialize("ny", "2026-11-03", "2026-11-06")
        # 时钟停在停机窗口内：触发尝试全部失败，窗口保持未完成、不留虚假覆盖
        clock.set(_epoch("2026-11-04T12:00:00Z"))
        failed = service.scheduler.tick(max_runs=10, max_readings=100)
        failed_status = [
            {"business_date": w["business_date"], "status": w["status"],
             "attempts": w["attempts"]}
            for w in service.scheduler.windows("ny")
            if "2026-11-03" <= w["business_date"] <= "2026-11-06"
        ]
        # 恢复：移除停机，时钟拨到 11-08，按 all 策略逐日追赶
        provider.downtime.clear()
        clock.set(_epoch("2026-11-08T12:00:00Z"))
        catchup = service.scheduler.backfill(
            "ny", "2026-11-03", "2026-11-06", mode="all", max_runs=20, max_readings=100
        )
        report["chapters"].append({
            "chapter": "5_停机与逐日追赶",
            "downtime_window": {"start": down_start, "end": down_end},
            "tick_during_outage": failed,
            "status_during_outage": failed_status,
            "catchup": catchup,
            "window_status_after": [
                {"business_date": w["business_date"], "status": w["status"]}
                for w in service.scheduler.windows("ny")
                if "2026-11-03" <= w["business_date"] <= "2026-11-06"
            ],
        })

        # ---- 第 6 章：来源真实缺口 -> 不伪装完成，操作员明确放弃并留痕 ----
        hole_window = "ny.2026-11-02"
        # 时钟已在 11-08，11-02 的数据本应齐备；来源缺口让覆盖证据无法成立
        service.scheduler.tick(max_runs=10, max_readings=100)
        before = service.scheduler.coverage("ny", "2026-11-02", "2026-11-02")
        service.scheduler.abandon_gap(hole_window, "来源电表存储损坏，两小时无法恢复")
        after = service.scheduler.coverage("ny", "2026-11-02", "2026-11-02")

        # 节假日：感恩节物化后直接 skipped，不产生任何来源请求
        requests_before_holiday = len(provider.requests)
        service.scheduler.materialize("ny", "2026-11-26", "2026-11-26")
        holiday_window = service.scheduler.window("ny.2026-11-26")
        report["chapters"].append({
            "chapter": "6_真实缺口与明确放弃",
            "before_abandon_missing_seconds": before["totals"]["missing_seconds"],
            "after": after["windows"][0],
            "abandon_notes": [
                n for n in service.scheduler.notes("ny", "2026-11-02")
                if n["kind"] == "abandoned_gap"
            ],
            "holiday_window": {
                "business_date": holiday_window["business_date"],
                "status": holiday_window["status"],
            },
            "holiday_requested_source": len(provider.requests) > requests_before_holiday,
        })

        # ---- 第 7 章：latest 追赶策略在独立计划上演示 ----
        # 同样停了几天：latest 只采最近一个营业日，更早的日期显式放弃并留原因
        service.scheduler.create_plan("ny-latest", "ny-plant", {
            "zone": "America/New_York",
            "business_start": "02:00",
            "trigger": "02:30",
            "ambiguous": "first",
            "nonexistent": "shift-forward",
            "segment": "ny-seg",
            "endpoint": base,
            "catchup": "latest",
        })
        latest = service.scheduler.backfill(
            "ny-latest", "2026-11-03", "2026-11-06", mode="latest",
            max_runs=20, max_readings=100,
        )
        latest_windows = [
            {"business_date": w["business_date"], "status": w["status"]}
            for w in service.scheduler.windows("ny-latest")
        ]
        latest_notes = service.scheduler.notes("ny-latest", "2026-11-03")
        report["chapters"].append({
            "chapter": "7_latest追赶_旧日期显式放弃",
            "catchup": latest,
            "windows": latest_windows,
            "abandon_note": [n["detail"]["code"] for n in latest_notes],
        })

        # ---- 第 8 章：对照请求区间、实际读数与放弃缺口 ----
        coverage = service.scheduler.coverage("ny", "2026-03-07", "2026-11-26")
        report["chapters"].append({
            "chapter": "8_总对照",
            "gateway_requests": list(provider.requests),
            "stored_reading_count": len(service.readings.segment("ny-seg")),
            "totals": coverage["totals"],
            "windows": [
                {
                    "business_date": row["business_date"],
                    "status": row["status"],
                    "revision": row["revision"],
                    "covered_seconds": row["covered_seconds"],
                    "missing_seconds": row["missing_seconds"],
                    "missing": row["missing"],
                }
                for row in coverage["windows"]
            ],
            "audit": service.store.verify_audit(),
        })
        report["ok"] = True

    if write_file:
        out = Path("var")
        out.mkdir(exist_ok=True)
        target = out / "collection_demo_report.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        report["report_path"] = str(target)
    return report


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
