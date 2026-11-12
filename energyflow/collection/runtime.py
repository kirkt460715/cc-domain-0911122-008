"""可注入时钟与来源客户端，演示无需真的等待一个营业日。"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..values import integer, reject
from .localtime import iso_utc


class Clock:
    """固定起步、可手工推进的时钟；默认绑定真实 wall clock 也可整体替换。"""

    def __init__(self, start=None):
        self.offset = 0
        if start is not None:
            self.fixed = integer(start)
        else:
            self.fixed = None

    def __call__(self):
        if self.fixed is None:
            return int(datetime.now(timezone.utc).timestamp()) + self.offset
        return self.fixed + self.offset

    def advance(self, seconds):
        self.offset += integer(seconds, 0, 10 * 365 * 86400)
        return self()

    def set(self, instant_value):
        self.fixed = integer(instant_value)
        self.offset = 0
        return self()


class SourceClient:
    """本地网关客户端。时钟/端点均可注入，网络调用只发生在回环地址。

    网关协议（见 docs/来源协议.md）：complete=true 且 coverage 非空才证明
    整个请求区间已覆盖；rows 里有读数但 complete=false 不能当作覆盖证据。
    """

    def __init__(self, transport=None, timeout=5):
        self.transport = transport or urllib.request.urlopen
        self.timeout = timeout

    def fetch(self, endpoint, meter, segment, start, end, max_readings=100):
        if not endpoint:
            reject("input", "collection plan has no endpoint configured")
        start, end = integer(start), integer(end)
        if end <= start:
            reject("time", "source request needs a positive interval")
        query = urllib.parse.urlencode(
            {
                "meter": meter,
                "segment": segment or "segment1",
                "start": iso_utc(start),
                "end": iso_utc(end),
                "limit": integer(max_readings, 1, 1000),
            }
        )
        request = urllib.request.Request(
            endpoint.rstrip("/") + "/readings?" + query,
            headers={"Accept": "application/json"},
        )
        try:
            with self.transport(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            reject("source", "gateway rejected request", status=exc.code)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            reject("source", "gateway unreachable or unreadable", reason=str(exc))
        return self._normalize(body, start, end)

    @staticmethod
    def _normalize(body, start, end):
        if not isinstance(body, dict) or "rows" not in body:
            reject("source", "gateway response missing rows")
        rows = []
        for row in body["rows"]:
            at_text = row.get("at")
            parsed = datetime.fromisoformat(at_text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                reject("source", "gateway reading lacks UTC offset")
            rows.append(
                {
                    "id": str(row["id"]),
                    "at": int(parsed.timestamp()),
                    "value": int(row["value"]),
                    "kind": row.get("kind", "normal"),
                }
            )
        coverage = None
        if body.get("complete") and body.get("coverage"):
            cov = body["coverage"]
            c_start = int(
                datetime.fromisoformat(
                    cov["start"].replace("Z", "+00:00")
                ).timestamp()
            )
            c_end = int(
                datetime.fromisoformat(
                    cov["end"].replace("Z", "+00:00")
                ).timestamp()
            )
            coverage = {"start": c_start, "end": c_end}
        return {
            "rows": rows,
            "complete": bool(body.get("complete")),
            "coverage": coverage,
        }


class ScriptedClient:
    """测试用假客户端：按脚本返回，记录请求区间。"""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def fetch(self, endpoint, meter, segment, start, end, max_readings=100):
        self.requests.append({"start": start, "end": end, "meter": meter})
        return self.responder(start, end, max_readings)
