"""采集窗口的物化、执行、追赶与覆盖核对。

所有覆盖范围都落在 UTC 时间轴；当地日期仅作为业务标签。窗口一经物化即绑定
计划版本，后续计划改版不会重新解释它。区间所有权以已成功运行的覆盖并集为准，
正常触发与人工补采靠同一套所有权规则协调，互不重复请求。
"""

import json
from datetime import datetime, timedelta

from ..values import canonical, fingerprint, integer, key, reject, text
from ..calendar import subtract
from .localtime import (
    business_day_span,
    daterange,
    explain_ambiguous,
    explain_nonexistent,
    iso_utc,
    load_zone,
    parse_date,
    resolve_wall,
)

TERMINAL_STATUSES = {"collected", "abandoned", "skipped"}
ACTIVE_STATUSES = {"planned", "ready", "running", "partial", "failed"}


class CollectionEngine:
    def __init__(self, store, plans, clock, client, readings):
        self.store = store
        self.plans = plans
        self.clock = clock
        self.client = client
        self._readings = readings

    # ---------- 单日展开（纯日历计算，预览与物化共用） ----------

    def expand_day(self, plan, business_date):
        zone = load_zone(plan["definition"]["zone"])
        definition = plan["definition"]
        span = business_day_span(
            business_date, definition["business_start_seconds"], zone
        )
        trigger_resolved = resolve_wall(
            business_date, definition["trigger_seconds"], zone
        )
        triggers = []
        notes = list(span["notes"])
        if trigger_resolved["state"] == "unique":
            triggers = [
                {
                    "at": trigger_resolved["instants"][0],
                    "ordinal": 0,
                    "interpretation": "unique",
                }
            ]
        elif trigger_resolved["state"] == "gap":
            explained = explain_nonexistent(trigger_resolved, definition["nonexistent"])
            if explained["action"] == "skip":
                notes.append(
                    {
                        "kind": "trigger",
                        "detail": explained["reason"],
                        "at": None,
                    }
                )
            else:
                triggers.append(
                    {
                        "at": explained["instants"][0],
                        "ordinal": 0,
                        "interpretation": "shift-forward",
                    }
                )
                notes.append(
                    {
                        "kind": "trigger",
                        "detail": explained["reason"],
                        "at": explained["instants"][0],
                    }
                )
        else:
            explained = explain_ambiguous(trigger_resolved, definition["ambiguous"])
            for ordinal, instant_value in enumerate(explained["instants"]):
                triggers.append(
                    {
                        "at": instant_value,
                        "ordinal": ordinal,
                        "interpretation": (
                            "ambiguous-first" if ordinal == 0 else "ambiguous-second"
                        ),
                    }
                )
            for dropped in explained["dropped"]:
                notes.append(
                    {
                        "kind": "trigger",
                        "detail": {
                            "code": "ambiguous_local_time_dropped",
                            "instant": dropped,
                            "policy": definition["ambiguous"],
                        },
                        "at": dropped,
                    }
                )
        clips = self.plans.maintenance_clips(
            definition, zone, span["start"], span["end"]
        )
        holiday = self.plans.is_holiday(definition, business_date)
        return {
            "business_date": business_date.isoformat(),
            "start": span["start"],
            "end": span["end"],
            "seconds": span["seconds"],
            "triggers": triggers,
            "maintenance_clips": clips,
            "holiday": holiday,
            "notes": notes,
        }

    # ---------- 预览（不落库） ----------

    def preview(self, plan_id, first_day, last_day, revision=None):
        plan = self.plans.get(plan_id, revision)
        first_day, last_day = parse_date(first_day), parse_date(last_day)
        if last_day < first_day:
            reject("input", "preview date range is reversed")
        rows = []
        for day in daterange(first_day, last_day):
            expanded = self.expand_day(plan, day)
            clips = [
                {"start": a, "end": b, "reason": reason}
                for a, b, reason in expanded["maintenance_clips"]
            ]
            rows.append(
                {
                    "business_date": expanded["business_date"],
                    "utc_start": iso_utc(expanded["start"]),
                    "utc_end": iso_utc(expanded["end"]),
                    "seconds": expanded["seconds"],
                    "holiday": expanded["holiday"],
                    "triggers": [
                        {
                            "at": iso_utc(item["at"]),
                            "interpretation": item["interpretation"],
                        }
                        for item in expanded["triggers"]
                    ],
                    "maintenance": clips,
                    "notes": expanded["notes"],
                }
            )
        return {"plan": plan_id, "revision": plan["revision"], "days": rows}

    # ---------- 物化 ----------

    @staticmethod
    def _window_id(plan_id, day_label, ordinal):
        # key() 标识符只允许字母数字与 _.:-，日期里的短横合法，用点分隔
        base = "%s.%s" % (plan_id, day_label)
        return base if not ordinal else "%s.%d" % (base, ordinal)

    def _add_note(self, con, plan_id, revision, day_label, note):
        ident = fingerprint(
            [plan_id, revision, day_label, note["kind"], canonical(note["detail"]), note["at"]]
        )[:16]
        con.execute(
            "INSERT OR IGNORE INTO collection_notes "
            "(id,plan_id,revision,business_date,kind,detail,at) VALUES(?,?,?,?,?,?,?)",
            (
                ident,
                plan_id,
                revision,
                day_label,
                note["kind"],
                canonical(note["detail"]),
                note["at"],
            ),
        )

    def materialize(self, plan_id, first_day, last_day):
        plan = self.plans.get(plan_id)
        first_day, last_day = parse_date(first_day), parse_date(last_day)
        created = []
        with self.store.atomic() as con:
            for day in daterange(first_day, last_day):
                expanded = self.expand_day(plan, day)
                day_label = expanded["business_date"]
                existing = self.store.one(
                    "SELECT id FROM collection_windows WHERE plan_id=? AND business_date=?",
                    (plan_id, day_label),
                )
                if existing:
                    continue
                for note in expanded["notes"]:
                    self._add_note(con, plan_id, plan["revision"], day_label, note)
                if expanded["holiday"]:
                    self._insert_window(
                        con,
                        plan,
                        day_label,
                        expanded["start"],
                        expanded["end"],
                        None,
                        0,
                        "skipped",
                        [
                            {
                                "kind": "holiday",
                                "detail": {"code": "business_date_is_holiday"},
                                "at": None,
                            }
                        ],
                        expanded,
                    )
                    continue
                if not expanded["triggers"]:
                    # 触发挂钟不存在且策略为 skip：整窗放弃并写明原因
                    self._insert_window(
                        con,
                        plan,
                        day_label,
                        expanded["start"],
                        expanded["end"],
                        None,
                        0,
                        "abandoned",
                        [
                            {
                                "kind": "trigger",
                                "detail": {
                                    "code": "window_abandoned_trigger_skipped"
                                },
                                "at": None,
                            }
                        ],
                        expanded,
                    )
                    continue
                for trigger in expanded["triggers"]:
                    # 只有维护段完全覆盖营业日才整日放弃；部分维护保留窗口，
                    # 维护区间作为排除段参与覆盖核对，tick 时不会请求这些时段。
                    fully_excluded = any(
                        a <= expanded["start"] and b >= expanded["end"]
                        for a, b, _ in expanded["maintenance_clips"]
                    )
                    status = "planned"
                    window_notes = []
                    if fully_excluded:
                        status = "abandoned"
                        window_notes.append(
                            {
                                "kind": "maintenance",
                                "detail": {"code": "window_inside_full_day_maintenance"},
                                "at": trigger["at"],
                            }
                        )
                    elif expanded["maintenance_clips"]:
                        window_notes.append(
                            {
                                "kind": "maintenance",
                                "detail": {
                                    "code": "maintenance_excludes_part_of_window",
                                    "clips": [
                                        {"start": a, "end": b, "reason": reason}
                                        for a, b, reason in expanded["maintenance_clips"]
                                    ],
                                },
                                "at": trigger["at"],
                            }
                        )
                    self._insert_window(
                        con,
                        plan,
                        day_label,
                        expanded["start"],
                        expanded["end"],
                        trigger["at"],
                        trigger["ordinal"],
                        status,
                        window_notes,
                        expanded,
                    )
                created.append(day_label)
        return {"plan": plan_id, "revision": plan["revision"], "materialized": created}

    def _insert_window(self, con, plan, day_label, start, end, trigger_at,
                       ordinal, status, extra_notes, expanded):
        ident = self._window_id(plan["id"], day_label, ordinal)
        for note in extra_notes:
            self._add_note(con, plan["id"], plan["revision"], day_label, note)
        payload = {
            "id": ident,
            "plan_id": plan["id"],
            "revision": plan["revision"],
            "meter": plan["meter"],
            "business_date": day_label,
            "start": start,
            "end": end,
            "trigger_at": trigger_at,
            "ordinal": ordinal,
            "status": status,
            "maintenance_clips": [
                {"start": a, "end": b, "reason": reason}
                for a, b, reason in expanded["maintenance_clips"]
            ],
        }
        con.execute(
            "INSERT INTO collection_windows "
            "(id,plan_id,revision,meter,business_date,start,end,trigger_at,status,"
            "attempts,note,payload,digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident,
                plan["id"],
                plan["revision"],
                plan["meter"],
                day_label,
                start,
                end,
                trigger_at,
                status,
                0,
                canonical(extra_notes),
                canonical(payload),
                fingerprint(payload),
            ),
        )
        self.store.event("collection_window_materialized", ident, {
            "status": status,
            "business_date": day_label,
            "revision": plan["revision"],
        })

    # ---------- 所有权与覆盖 ----------

    def _covered_intervals(self, window_id):
        rows = self.store.rows(
            "SELECT covered FROM collection_runs WHERE window_id=? AND status='succeeded' "
            "ORDER BY id",
            (window_id,),
        )
        intervals = []
        for row in rows:
            for item in json.loads(row["covered"]):
                intervals.append((item["start"], item["end"]))
        return intervals

    def _excluded_intervals(self, window_id):
        row = self.store.one(
            "SELECT payload FROM collection_windows WHERE id=?", (window_id,)
        )
        payload = json.loads(row["payload"])
        return [(item["start"], item["end"]) for item in payload["maintenance_clips"]]

    def _missing_intervals(self, window_id):
        window = self.get_window(window_id)
        target = [(window["start"], window["end"])]
        # 先扣除维护排除段
        work = []
        for a, b in target:
            work.extend(subtract(a, b, self._excluded_intervals(window_id)))
        remaining = []
        for a, b in work:
            remaining.extend(subtract(a, b, self._covered_intervals(window_id)))
        return remaining

    def _sibling_owned_intervals(self, window):
        """同一计划其他窗口已拥有的覆盖区间（both 策略的重复窗口借此共享所有权）。"""
        rows = self.store.rows(
            "SELECT c.covered FROM collection_runs c JOIN collection_windows w "
            "ON c.window_id=w.id WHERE w.plan_id=? AND w.id<>? AND c.status='succeeded'",
            (window["plan_id"], window["id"]),
        )
        intervals = []
        for row in rows:
            for item in json.loads(row["covered"]):
                intervals.append((item["start"], item["end"]))
        return intervals

    def _requestable_intervals(self, window):
        """本窗口仍需向来源申请的区间：扣除维护段、自身与兄弟窗口的已拥有区间。"""
        remaining = self._missing_intervals(window["id"])
        sibling = self._sibling_owned_intervals(window)
        if not sibling:
            return remaining
        result = []
        for a, b in remaining:
            result.extend(subtract(a, b, sibling))
        return result

    def get_window(self, window_id):
        row = self.store.one(
            "SELECT * FROM collection_windows WHERE id=?", (text(window_id),)
        )
        if row is None:
            reject("missing", "collection window not found", window=window_id)
        payload = json.loads(row["payload"])
        if fingerprint(payload) != row["digest"]:
            reject("integrity", "collection window payload changed", window=window_id)
        # 状态、尝试次数是运行期推进的列；payload 保留物化时的不可变定义
        payload["status"] = row["status"]
        payload["attempts"] = row["attempts"]
        payload["stored_note"] = json.loads(row["note"])
        return payload

    def _set_status(self, con, window_id, status):
        """状态列与 payload 摘要一起推进，审计上仍能区分物化版本与运行状态。"""
        row = self.store.one(
            "SELECT payload FROM collection_windows WHERE id=?", (window_id,)
        )
        payload = json.loads(row["payload"])
        payload["status"] = status
        con.execute(
            "UPDATE collection_windows SET status=?,payload=?,digest=? WHERE id=?",
            (status, canonical(payload), fingerprint(payload), window_id),
        )

    def list_windows(self, plan_id=None, status=None, limit=500):
        integer(limit, 1, 5000)
        clauses, arguments = [], []
        if plan_id is not None:
            clauses.append("plan_id=?")
            arguments.append(key(plan_id))
        if status is not None:
            clauses.append("status=?")
            arguments.append(text(status))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.store.rows(
            "SELECT id FROM collection_windows" + where +
            " ORDER BY start,id LIMIT ?",
            tuple(arguments) + (limit,),
        )
        return [self.get_window(row["id"]) for row in rows]

    def notes(self, plan_id, business_date=None):
        clauses, arguments = ["plan_id=?"], [key(plan_id)]
        if business_date is not None:
            clauses.append("business_date=?")
            arguments.append(parse_date(business_date).isoformat())
        rows = self.store.rows(
            "SELECT * FROM collection_notes WHERE " + " AND ".join(clauses) +
            " ORDER BY business_date,at",
            tuple(arguments),
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    # ---------- 执行 ----------

    def _due_windows(self, now, limit):
        rows = self.store.rows(
            "SELECT id FROM collection_windows WHERE status IN ('planned','ready','partial','failed') "
            "AND trigger_at IS NOT NULL AND trigger_at<=? ORDER BY trigger_at,id LIMIT ?",
            (now, integer(limit, 1, 1000)),
        )
        return [self.get_window(row["id"]) for row in rows]

    def _release_stale_leases(self, now):
        rows = self.store.rows(
            "SELECT id FROM collection_runs WHERE status='running' AND lease_expires_at<?",
            (now,),
        )
        for row in rows:
            with self.store.atomic() as con:
                run = self.store.one(
                    "SELECT * FROM collection_runs WHERE id=? AND status='running'",
                    (row["id"],),
                )
                if run is None:
                    continue
                con.execute(
                    "UPDATE collection_runs SET status='failed',reason=? WHERE id=?",
                    (canonical({"code": "lease_expired"}), row["id"]),
                )
                window = self.store.one(
                    "SELECT status FROM collection_windows WHERE id=?",
                    (run["window_id"],),
                )
                if window and window["status"] == "running":
                    self._set_status(con, run["window_id"], "failed")

    def tick(self, max_runs=5, max_readings=100, now=None):
        """到点执行一次调度；预算耗尽即停，下个 tick 从剩余区间继续。"""
        now = self.clock() if now is None else integer(now)
        integer(max_runs, 1, 1000)
        integer(max_readings, 1, 100000)
        self._release_stale_leases(now)
        report = {"ran": 0, "readings": 0, "windows": [], "budget_exhausted": False}
        for window in self._due_windows(now, max_runs * 4):
            # 同一窗口在一个 tick 内逐段追完：维护排除段与兄弟窗口已拥有区间
            # 都会被扣除，每次只申请一个真正无人拥有的区间。
            while True:
                requestable = self._requestable_intervals(window)
                if not requestable:
                    self._complete_if_possible(window["id"])
                    break
                if report["ran"] >= max_runs:
                    report["budget_exhausted"] = True
                    break
                request_start, request_end = requestable[0]
                outcome = self._run_once(
                    window, request_start, request_end, "scheduled", "tick",
                    max_readings, now,
                )
                report["ran"] += 1
                report["readings"] += outcome["readings"]
                report["windows"].append(
                    {"window": window["id"], "run": outcome["id"],
                     "status": outcome["status"]}
                )
                if outcome["status"] in ("failed", "incomplete"):
                    # 失败或来源未给出覆盖证据：本 tick 内不再重试同一窗口，
                    # 窗口保留 ready/partial 状态，下个 tick 或人工补采再继续。
                    break
            if report["budget_exhausted"]:
                break
        return report

    def _run_once(self, window, request_start, request_end, kind, owner,
                  max_readings, now, lease_seconds=300):
        plan = self.plans.get(window["plan_id"], window["revision"])
        definition = plan["definition"]
        prior = self.store.one(
            "SELECT COUNT(*) AS n FROM collection_runs WHERE window_id=?",
            (window["id"],),
        )["n"]
        run_id = "run.%s.%d" % (window["id"], prior + 1)
        with self.store.atomic() as con:
            blocked = self.store.one(
                "SELECT id FROM collection_runs WHERE window_id=? AND status='running'",
                (window["id"],),
            )
            if blocked:
                reject("state", "window already has a running attempt", window=window["id"])
            con.execute(
                "UPDATE collection_windows SET attempts=attempts+1 WHERE id=?",
                (window["id"],),
            )
            self._set_status(con, window["id"], "running")
            run_payload = {
                "id": run_id,
                "window_id": window["id"],
                "kind": kind,
                "owner": owner,
                "request_start": request_start,
                "request_end": request_end,
            }
            con.execute(
                "INSERT INTO collection_runs "
                "(id,window_id,kind,owner,request_start,request_end,started_at,"
                "finished_at,status,readings,covered,reason,payload,digest,lease_expires_at) "
                "VALUES(?,?,?,?,?,?,?,NULL,'running',0,'[]',NULL,?,?,?)",
                (
                    run_id, window["id"], kind, owner, request_start, request_end, now,
                    canonical(run_payload), fingerprint(run_payload), now + lease_seconds,
                ),
            )
        try:
            response = self.client.fetch(
                endpoint=definition["endpoint"],
                meter=window["meter"],
                segment=definition["segment"],
                start=request_start,
                end=request_end,
                max_readings=max_readings,
            )
        except Exception as exc:  # 网关故障：保留窗口，稍后重试
            return self._fail_run(run_id, window["id"],
                                  {"code": "source_unavailable", "message": str(exc)})
        return self._absorb_response(run_id, window, response, request_start, request_end)

    def _absorb_response(self, run_id, window, response, request_start, request_end):
        stored = []
        errors = []
        with self.store.atomic() as con:
            for row in response.get("rows", []):
                try:
                    result = self._ingest_reading(window, row)
                    stored.append(result)
                except Exception as exc:  # 单条坏数据不拖垮整次运行
                    errors.append({"id": row.get("id"), "reason": str(exc)})
            complete = bool(response.get("complete")) and response.get("coverage") is not None
            covered = []
            if complete:
                coverage = response["coverage"]
                covered = [
                    {
                        "start": max(request_start, coverage["start"]),
                        "end": min(request_end, coverage["end"]),
                    }
                ]
                covered = [c for c in covered if c["end"] > c["start"]]
            reason = None
            if errors:
                reason = {"code": "partial_rows_rejected", "errors": errors[:10]}
            status = "succeeded" if complete else "incomplete"
            run_row = self.store.one(
                "SELECT payload FROM collection_runs WHERE id=?", (run_id,)
            )
            payload = json.loads(run_row["payload"])
            payload.update(
                status=status,
                readings=len(stored),
                covered=covered,
                reason=reason,
                complete_evidence=complete,
            )
            con.execute(
                "UPDATE collection_runs SET finished_at=?,status=?,readings=?,covered=?,"
                "reason=?,payload=?,digest=? WHERE id=?",
                (
                    self.clock(),
                    status,
                    len(stored),
                    canonical(covered),
                    canonical(reason) if reason else None,
                    canonical(payload),
                    fingerprint(payload),
                    run_id,
                ),
            )
            self.store.event("collection_run_finished", run_id, {
                "status": status,
                "readings": len(stored),
                "evidence": complete,
            })
        self._complete_if_possible(window["id"])
        return {"id": run_id, "status": status, "readings": len(stored), "errors": errors}

    def _ingest_reading(self, window, row):
        # 复用既有读数入口：同一 (segment,at) 由 readings 层保证唯一，
        # 重复补采只会复用既有读数，不会覆盖手工暂停/修正过的值。
        segment = self.plans.get(window["plan_id"], window["revision"])["definition"]["segment"]
        # 网关来源 id 可能含 key() 不接受的字符，用短哈希稳定派生本地读数 id；
        # 同一来源读数重复补采会命中同一 id，由 readings 层复用而非覆盖。
        source_id = text(row["id"], 180)
        safe = fingerprint([segment, source_id])[:24]
        reading = {
            "id": "col-" + safe,
            "segment": segment,
            "at": row["at"],
            "value": integer(row["value"], 0, 10**12),
            "kind": row.get("kind", "normal"),
            "note": "collected by " + window["id"],
        }
        return self._readings.add(reading)
    def _fail_run(self, run_id, window_id, reason):
        with self.store.atomic() as con:
            row = self.store.one("SELECT payload FROM collection_runs WHERE id=?", (run_id,))
            payload = json.loads(row["payload"])
            payload.update(status="failed", reason=reason)
            con.execute(
                "UPDATE collection_runs SET finished_at=?,status='failed',reason=?,"
                "payload=?,digest=? WHERE id=?",
                (
                    self.clock(),
                    canonical(reason),
                    canonical(payload),
                    fingerprint(payload),
                    run_id,
                ),
            )
            current = self.store.one(
                "SELECT status FROM collection_windows WHERE id=?", (window_id,)
            )
            if current and current["status"] == "running":
                self._set_status(con, window_id, "failed")
        return {"id": run_id, "status": "failed", "readings": 0, "errors": [reason]}

    def _complete_if_possible(self, window_id):
        with self.store.atomic() as con:
            window = self.get_window(window_id)
            if window["status"] in TERMINAL_STATUSES:
                return window["status"]
            # 自己或兄弟窗口（both 策略的重复窗口）已拥有全部可申请区间即完成
            requestable = self._requestable_intervals(window)
            if not requestable:
                self._set_status(con, window_id, "collected")
                self.store.event("collection_window_collected", window_id, {})
                return "collected"
            missing = self._missing_intervals(window_id)
            has_success = self.store.one(
                "SELECT id FROM collection_runs WHERE window_id=? AND status='succeeded'",
                (window_id,),
            )
            current = self.store.one(
                "SELECT status FROM collection_windows WHERE id=?", (window_id,)
            )["status"]
            if has_success and missing and current != "partial":
                self._set_status(con, window_id, "partial")
                return "partial"
            if current in ("running", "failed", "planned"):
                self._set_status(con, window_id, "ready")
                return "ready"
            return current

    # ---------- 人工补采与放弃 ----------

    def backfill(self, plan_id, first_day=None, last_day=None, mode="all",
                 max_runs=10, max_readings=100, now=None):
        """逐日追赶（all）或只取最近一段（latest）。

        latest 模式下，最近营业日之前未物化的日子显式放弃并留原因，
        绝不把未采范围显示成已完成。
        """
        plan = self.plans.get(plan_id)
        now = self.clock() if now is None else integer(now)
        if mode not in {"all", "latest"}:
            reject("input", "backfill mode must be all or latest")
        zone = load_zone(plan["definition"]["zone"])
        today = datetime.fromtimestamp(now, zone).date()
        last_date = parse_date(last_day) if last_day else today
        first_date = parse_date(first_day) if first_day else last_date - timedelta(days=30)
        if last_date < first_date:
            reject("input", "backfill date range is reversed")
        self.materialize(plan_id, first_date.isoformat(), last_date.isoformat())
        if mode == "latest":
            self._abandon_older_than_latest(plan_id, last_date)
        result = self.tick(max_runs=max_runs, max_readings=max_readings, now=now)
        result["mode"] = mode
        return result

    def _abandon_older_than_latest(self, plan_id, last_date):
        with self.store.atomic() as con:
            rows = self.store.rows(
                "SELECT id,business_date,status FROM collection_windows WHERE plan_id=? "
                "AND business_date<? ORDER BY business_date",
                (plan_id, last_date.isoformat()),
            )
            for row in rows:
                if row["status"] in TERMINAL_STATUSES:
                    continue
                self._set_status(con, row["id"], "abandoned")
                self._add_note(con, plan_id, self.plans.get(plan_id)["revision"],
                               row["business_date"],
                               {"kind": "catchup",
                                "detail": {"code": "latest_catchup_older_day_abandoned"},
                                "at": None})
                self.store.event("collection_window_abandoned", row["id"],
                                 {"reason": "latest_catchup"})

    def mark_gap_abandoned(self, window_id, reason, now=None):
        """操作员明确放弃当前仍缺失的区间，缺口留痕而不是伪装完成。"""
        now = self.clock() if now is None else integer(now)
        reason = text(reason, 500)
        with self.store.atomic() as con:
            window = self.get_window(window_id)
            missing = self._missing_intervals(window_id)
            if not missing:
                reject("state", "window has no missing interval to abandon")
            for index, (a, b) in enumerate(missing):
                self._add_note(
                    con, window["plan_id"], window["revision"],
                    window["business_date"],
                    {"kind": "abandoned_gap",
                     "detail": {"code": "operator_abandoned_gap", "reason": reason,
                                "start": a, "end": b, "index": index},
                     "at": now},
                )
            self._set_status(con, window_id, "abandoned")
            self.store.event("collection_window_abandoned", window_id, {"reason": reason})
        return self.get_window(window_id)

    def runs(self, window_id=None):
        clauses, arguments = [], []
        if window_id is not None:
            clauses.append("window_id=?")
            arguments.append(text(window_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.store.rows(
            "SELECT * FROM collection_runs" + where + " ORDER BY started_at,id",
            tuple(arguments),
        )
        for row in rows:
            row["covered"] = json.loads(row["covered"])
            row["reason"] = json.loads(row["reason"]) if row["reason"] else None
            row["payload"] = json.loads(row["payload"])
        return rows

    def coverage_report(self, plan_id, first_day=None, last_day=None):
        windows = self.list_windows(plan_id, limit=5000)
        if first_day is not None:
            first_day = parse_date(first_day).isoformat()
            windows = [w for w in windows if w["business_date"] >= first_day]
        if last_day is not None:
            last_day = parse_date(last_day).isoformat()
            windows = [w for w in windows if w["business_date"] <= last_day]
        rows = []
        totals = {"requested_seconds": 0, "covered_seconds": 0,
                  "excluded_seconds": 0, "missing_seconds": 0, "abandoned_seconds": 0}
        all_covered, all_excluded, all_missing = [], [], []
        for window in windows:
            missing = self._missing_intervals(window_id=window["id"])
            # 展示口径：兄弟窗口已采的区间不算本窗口缺失
            requestable = self._requestable_intervals(window)
            excluded = self._excluded_intervals(window["id"])
            covered = self._covered_intervals(window["id"])
            missing_seconds = sum(b - a for a, b in requestable)
            excluded_seconds = sum(b - a for a, b in excluded)
            covered_seconds = sum(b - a for a, b in covered)
            totals["requested_seconds"] += window["end"] - window["start"]
            # 相邻营业日（尤其 DST 25 小时日）在 UTC 轴上可能相交，
            # 汇总按并集计秒，避免重复计算覆盖/排除/缺失。
            all_covered.extend(covered)
            all_covered.extend(self._sibling_owned_intervals(window))
            all_excluded.extend(excluded)
            all_missing.extend(requestable)
            if window["status"] == "abandoned":
                totals["abandoned_seconds"] += missing_seconds
            rows.append({
                "window": window["id"],
                "business_date": window["business_date"],
                "revision": window["revision"],
                "status": window["status"],
                "utc_start": iso_utc(window["start"]),
                "utc_end": iso_utc(window["end"]),
                "covered": [{"start": iso_utc(a), "end": iso_utc(b)} for a, b in covered],
                "excluded": [{"start": iso_utc(a), "end": iso_utc(b)} for a, b in excluded],
                "missing": [{"start": iso_utc(a), "end": iso_utc(b)} for a, b in requestable],
                "covered_seconds": covered_seconds,
                "missing_seconds": missing_seconds,
                "excluded_seconds": excluded_seconds,
            })
        totals["covered_seconds"] = self._union_seconds(all_covered)
        totals["excluded_seconds"] = self._union_seconds(all_excluded)
        totals["missing_seconds"] = self._union_seconds(all_missing)
        return {"plan": plan_id, "windows": rows, "totals": totals,
                "complete": totals["missing_seconds"] == 0}

    @staticmethod
    def _union_seconds(intervals):
        ordered = sorted(intervals)
        merged = []
        for left, right in ordered:
            if not merged or left > merged[-1][1]:
                merged.append([left, right])
            else:
                merged[-1][1] = max(merged[-1][1], right)
        return sum(right - left for left, right in merged)
