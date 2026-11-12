"""本地挂钟时间到 UTC 时间轴的确定性解析。

园区只按当地营业日交数据，当地日期只是业务标签；这里负责把当地挂钟时间
映射到真正的 UTC 时刻，并把夏令时造成的不存在/重复时刻显式暴露出来。
"""

from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..values import reject, integer, text

AMBIGUOUS_POLICIES = {"first", "second", "both"}
NONE_POLICIES = {"skip", "shift-forward"}


def load_zone(name):
    name = text(name, 64)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        reject("timezone", "unknown IANA time zone", zone=name)


def parse_clock(value):
    """接受 HH:MM:SS 或 HH:MM，返回当天秒数 [0,86400)。"""
    value = text(value, 8)
    parts = value.split(":")
    if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts):
        reject("input", "local clock time must look like HH:MM[:SS]")
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if hour > 23 or minute > 59 or second > 59:
        reject("input", "local clock time outside one day")
    return hour * 3600 + minute * 60 + second


def clock_text(seconds):
    seconds = integer(seconds, 0, 86399)
    return "%02d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def parse_date(value):
    value = text(value, 10)
    try:
        return date.fromisoformat(value)
    except ValueError:
        reject("input", "business date must be ISO YYYY-MM-DD", value=value)


def _wall(local_date, clock_seconds, zone, fold):
    base = datetime.combine(local_date, time(0, 0, 0), tzinfo=zone)
    moment = base + timedelta(seconds=clock_seconds)
    return moment.replace(fold=fold)


def resolve_wall(local_date, clock_seconds, zone):
    """解析单个挂钟时间。

    返回字典：
      state: unique | gap | ambiguous
      instants: 一个或两个（fold=0,fold=1）UTC 秒
      gap: 缺口区间 [gap_start,gap_end)（仅 gap）
      offsets: 各候选对应的 UTC 偏移秒数
    """
    integer(clock_seconds, 0, 86399)
    first = _wall(local_date, clock_seconds, zone, 0)
    second = _wall(local_date, clock_seconds, zone, 1)
    t0, t1 = int(first.timestamp()), int(second.timestamp())
    off0, off1 = int(first.utcoffset().total_seconds()), int(
        second.utcoffset().total_seconds()
    )
    if t0 == t1:
        return {
            "state": "unique",
            "instants": [t0],
            "offsets": [off0],
            "gap": None,
        }
    if t0 > t1:
        # 春季缺口：fold=0 挂在缺口后（UTC 较早），fold=1 挂在缺口前
        return {
            "state": "gap",
            "instants": [t1, t0],
            "offsets": [off1, off0],
            "gap": [t1, t0],
        }
    # 秋季重复：fold=0 是较早的夏令时时刻，fold=1 是较晚的标准时时刻
    return {
        "state": "ambiguous",
        "instants": [t0, t1],
        "offsets": [off0, off1],
        "gap": None,
    }


def explain_nonexistent(resolved, policy):
    """按 skip/shift-forward 解释不存在的挂钟时刻。"""
    if policy not in NONE_POLICIES:
        reject("input", "nonexistent policy must be skip or shift-forward")
    if resolved["state"] != "gap":
        return {"action": "as-is", "instants": resolved["instants"][:1], "reason": None}
    gap_start, gap_end = resolved["gap"]
    if policy == "skip":
        return {
            "action": "skip",
            "instants": [],
            "reason": {
                "code": "nonexistent_local_time_skipped",
                "gap_start": gap_start,
                "gap_end": gap_end,
            },
        }
    return {
        "action": "shift-forward",
        "instants": [gap_end],
        "reason": {
            "code": "nonexistent_local_time_shifted_forward",
            "gap_start": gap_start,
            "gap_end": gap_end,
        },
    }


def explain_ambiguous(resolved, policy):
    """按 first/second/both 解释重复的挂钟时刻。

    first 取偏移较大（较早、夏令时）的一次，second 取较晚的一次；
    both 保留两个 UTC 时刻，调用方负责各展开一次。
    """
    if policy not in AMBIGUOUS_POLICIES:
        reject("input", "ambiguous policy must be first, second or both")
    if resolved["state"] != "ambiguous":
        return {"instants": resolved["instants"][:1], "dropped": []}
    early, late = resolved["instants"]
    if policy == "first":
        return {"instants": [early], "dropped": [late]}
    if policy == "second":
        return {"instants": [late], "dropped": [early]}
    return {"instants": [early, late], "dropped": []}


def business_day_span(business_date, business_start_seconds, zone):
    """营业日 D 的覆盖范围：当地 D 日 business_start 到 D+1 日 business_start。

    边界若落在缺口或重复时刻，同样按 fold 显式求解并返回解释，
    保证覆盖范围永远是 UTC 轴上确定的左闭右开区间。
    """
    start_resolved = resolve_wall(business_date, business_start_seconds, zone)
    end_date = business_date + timedelta(days=1)
    end_resolved = resolve_wall(end_date, business_start_seconds, zone)

    def boundary(resolved):
        if resolved["state"] == "unique":
            return resolved["instants"][0], None
        if resolved["state"] == "gap":
            # 缺口边界：fold=0 是缺口前最后一刻，fold=1 是缺口后第一刻
            gap_start, gap_end = resolved["gap"]
            return gap_start, {
                "kind": "boundary",
                "detail": {
                    "code": "boundary_inside_spring_gap",
                    "gap_start": gap_start,
                    "gap_end": gap_end,
                    "resolution": "gap_start",
                },
                "at": gap_start,
            }
        # 重复边界取第一次出现（fold=0，较早的 UTC 时刻）
        return resolved["instants"][0], {
            "kind": "boundary",
            "detail": {
                "code": "boundary_ambiguous_first_used",
                "instants": resolved["instants"],
            },
            "at": resolved["instants"][0],
        }

    start, start_note = boundary(start_resolved)
    end, end_note = boundary(end_resolved)
    if end <= start:
        reject("calendar", "business day span did not advance on UTC axis")
    return {
        "start": start,
        "end": end,
        "seconds": end - start,
        "start_resolved": start_resolved,
        "end_resolved": end_resolved,
        "notes": [note for note in (start_note, end_note) if note],
    }


def iso_utc(instant_value):
    return (
        datetime.fromtimestamp(integer(instant_value), timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def local_label(instant_value, zone):
    return datetime.fromtimestamp(integer(instant_value), zone).isoformat()


def daterange(first, last):
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)
