from ..values import (
    canonical,
    fingerprint,
    integer,
    items,
    key,
    object_fields,
    reject,
    text,
)
from .localtime import (
    AMBIGUOUS_POLICIES,
    NONE_POLICIES,
    clock_text,
    load_zone,
    parse_clock,
    parse_date,
)

CATCHUP_POLICIES = {"all", "latest"}


class Plans:
    def __init__(self, store):
        self.store = store

    def _validate_window(self, window, zone):
        # 两种写法：{"start":"2026-11-01","end":"2026-11-02"} 整日；
        # 或 {"start":"2026-11-01T10:00","end":"2026-11-01T12:00"} 本地挂钟时段。
        object_fields(window, {"start", "end", "reason"}, {"start", "end"})
        start = text(window["start"], 40)
        end = text(window["end"], 40)
        reason = text(window.get("reason", "maintenance"), 300)
        if len(start) == 10 and len(end) == 10:
            left_day, right_day = parse_date(start), parse_date(end)
            if right_day < left_day:
                reject("input", "maintenance window ends before it starts")
            return {"start": start, "end": end, "all_day": True, "reason": reason}
        from datetime import datetime

        try:
            left = datetime.fromisoformat(start)
            right = datetime.fromisoformat(end)
        except ValueError:
            reject("input", "maintenance window must be local YYYY-MM-DD or ISO datetime")
        if left.tzinfo is not None or right.tzinfo is not None or right <= left:
            reject(
                "input",
                "maintenance partial windows use naive local wall time, end after start",
            )
        return {
            "start": left.isoformat(),
            "end": right.isoformat(),
            "all_day": False,
            "reason": reason,
        }

    def validate(self, definition):
        object_fields(
            definition,
            {
                "zone",
                "business_start",
                "trigger",
                "ambiguous",
                "nonexistent",
                "holidays",
                "maintenance",
                "catchup",
                "endpoint",
                "segment",
                "tick_seconds",
                "max_gap_seconds",
            },
            {"zone", "business_start", "trigger"},
        )
        zone = load_zone(definition["zone"])
        business_start = parse_clock(definition["business_start"])
        trigger = parse_clock(definition["trigger"])
        ambiguous = definition.get("ambiguous", "first")
        nonexistent = definition.get("nonexistent", "shift-forward")
        if ambiguous not in AMBIGUOUS_POLICIES:
            reject("input", "ambiguous policy must be first/second/both")
        if nonexistent not in NONE_POLICIES:
            reject("input", "nonexistent policy must be skip/shift-forward")
        holidays = definition.get("holidays", [])
        items(holidays, 0, 1000)
        holiday_set = sorted({parse_date(day).isoformat() for day in holidays})
        maintenance = definition.get("maintenance", [])
        items(maintenance, 0, 500)
        normalized_maintenance = [
            self._validate_window(window, zone) for window in maintenance
        ]
        catchup = definition.get("catchup", "all")
        if catchup not in CATCHUP_POLICIES:
            reject("input", "catchup policy must be all or latest")
        endpoint = text(definition.get("endpoint", ""), 500, True)
        segment = definition.get("segment")
        if segment is not None:
            segment = key(segment)
        tick_seconds = integer(definition.get("tick_seconds", 3600), 60, 7 * 86400)
        max_gap_seconds = integer(definition.get("max_gap_seconds", 3600), 60, 86400)
        return {
            "zone": definition["zone"],
            "business_start": clock_text(business_start),
            "business_start_seconds": business_start,
            "trigger": clock_text(trigger),
            "trigger_seconds": trigger,
            "ambiguous": ambiguous,
            "nonexistent": nonexistent,
            "holidays": holiday_set,
            "maintenance": normalized_maintenance,
            "catchup": catchup,
            "endpoint": endpoint,
            "segment": segment,
            "tick_seconds": tick_seconds,
            "max_gap_seconds": max_gap_seconds,
        }

    def create(self, plan_id, meter, definition, now):
        plan_id = key(plan_id)
        key(meter)
        payload = self.validate(definition)
        now = integer(now)
        with self.store.atomic() as con:
            latest = self.store.one(
                "SELECT revision FROM collection_plans WHERE id=? ORDER BY revision DESC LIMIT 1",
                (plan_id,),
            )
            revision = (latest["revision"] + 1) if latest else 1
            con.execute(
                "UPDATE collection_plans SET active=0 WHERE id=?", (plan_id,)
            )
            record = {
                "id": plan_id,
                "revision": revision,
                "meter": meter,
                "created_at": now,
                "definition": payload,
            }
            con.execute(
                "INSERT INTO collection_plans "
                "(id,revision,meter,created_at,active,payload,digest) "
                "VALUES(?,?,?,?,1,?,?)",
                (
                    plan_id,
                    revision,
                    meter,
                    now,
                    canonical(record),
                    fingerprint(record),
                ),
            )
            self.store.event(
                "collection_plan_version",
                plan_id,
                {"revision": revision, "meter": meter, "definition": payload},
            )
        return self.get(plan_id)

    def _row(self, plan_id, revision=None):
        if revision is None:
            row = self.store.one(
                "SELECT * FROM collection_plans WHERE id=? AND active=1", (plan_id,)
            )
        else:
            integer(revision, 1)
            row = self.store.one(
                "SELECT * FROM collection_plans WHERE id=? AND revision=?",
                (plan_id, revision),
            )
        if row is None:
            reject("missing", "collection plan version not found", plan=plan_id)
        return row

    def get(self, plan_id, revision=None):
        row = self._row(plan_id, revision)
        payload = __import__("json").loads(row["payload"])
        if fingerprint(payload) != row["digest"]:
            reject("integrity", "collection plan payload changed", plan=plan_id)
        return {**payload, "active": bool(row["active"])}

    def list_versions(self, plan_id):
        rows = self.store.rows(
            "SELECT revision,active,created_at,meter FROM collection_plans "
            "WHERE id=? ORDER BY revision",
            (key(plan_id),),
        )
        if not rows:
            reject("missing", "collection plan not found", plan=plan_id)
        return rows

    def list(self):
        rows = self.store.rows(
            "SELECT id,MAX(revision) AS revision FROM collection_plans GROUP BY id ORDER BY id"
        )
        return [{"id": row["id"], "revision": row["revision"]} for row in rows]

    def maintenance_clips(self, definition, zone, day_start, day_end):
        """把维护段折算成某个营业日 UTC 窗口内的排除区间（左闭右开）。"""
        from datetime import datetime

        day_label = datetime.fromtimestamp(day_start, zone).date().isoformat()
        clips = []
        for window in definition["maintenance"]:
            if window["all_day"]:
                if window["start"] <= day_label <= window["end"]:
                    clips.append((day_start, day_end, window["reason"]))
                continue
            left = datetime.fromisoformat(window["start"])
            right = datetime.fromisoformat(window["end"])
            # 维护挂钟段与当前营业日有交集才处理；gap 内的挂钟按 shift 到缺口后
            left_resolved = self._wall_to_instant(left, zone)
            right_resolved = self._wall_to_instant(right, zone)
            a = max(left_resolved, day_start)
            b = min(right_resolved, day_end)
            if b > a:
                clips.append((a, b, window["reason"]))
        return clips

    @staticmethod
    def _wall_to_instant(moment, zone):
        from .localtime import resolve_wall

        resolved = resolve_wall(moment.date(), moment.hour * 3600 +
                                moment.minute * 60 + moment.second, zone)
        if resolved["state"] == "gap":
            return resolved["gap"][1]
        if resolved["state"] == "ambiguous":
            return resolved["instants"][0]
        return resolved["instants"][0]

    def is_holiday(self, definition, business_date):
        return business_date.isoformat() in set(definition["holidays"])
