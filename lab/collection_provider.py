"""营业日采集网关（可注入时钟、缺口与停机的本地模拟器）。

与 lab.provider.Provider 一样只监听回环地址，不连接任何生产服务。
GET /readings 按左闭右开 UTC 区间返回合成小时读数；只有数据覆盖到整个请求
区间时 complete=true 且 coverage 非空，rows 存在但未铺满时 complete=false、
coverage=null——几条读数不等于全覆盖。

管理接口（演示用）：
  POST /admin/clock      {"at": <epoch>}          注入网关当前时间
  POST /admin/gap        {"start":..,"end":..}    声明来源侧真实数据缺口
  POST /admin/down       {"start":..,"end":..}    声明停机时段（期间请求 503）
  GET  /requests                                  查看实际请求区间
  POST /admin/reset                               清空注入与请求记录
"""

import datetime as dt
import threading
import urllib.parse

from .http import ProtocolError, main


def _parse(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class CollectionProvider:
    def __init__(self, clock=None, tick_seconds=3600):
        self.lock = threading.RLock()
        self.requests = []
        # clock 是零参数可调用对象，返回 epoch 秒；默认真实时间
        self.clock = clock or (lambda: int(dt.datetime.now(dt.timezone.utc).timestamp()))
        self.tick_seconds = tick_seconds
        self.gaps = []      # 来源侧真实缺口，其中没有读数，也不能声称覆盖
        self.downtime = []  # 停机时段，请求直接失败

    def handle(self, method, path, args, body):
        with self.lock:
            parsed = urllib.parse.urlsplit(path)
            route = parsed.path
            if route == "/requests":
                return self.requests
            if method == "POST" and route == "/admin/reset":
                self.requests.clear()
                self.gaps.clear()
                self.downtime.clear()
                return {"ok": True}
            if method == "POST" and route == "/admin/clock":
                self.clock = _fixed(body["at"])
                return {"ok": True, "at": body["at"]}
            if method == "POST" and route == "/admin/gap":
                self.gaps.append((_parse(body["start"]), _parse(body["end"])))
                return {"ok": True, "gaps": len(self.gaps)}
            if method == "POST" and route == "/admin/down":
                self.downtime.append((_parse(body["start"]), _parse(body["end"])))
                return {"ok": True, "downtime": len(self.downtime)}
            if route != "/readings":
                raise ProtocolError(404, "route")

            start = _parse(args["start"])
            end = _parse(args["end"])
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ProtocolError(400, "interval")
            limit = int(args.get("limit", "100"))
            now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
            item = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "meter": args["meter"],
            }
            self.requests.append(item)

            # 请求区间与任一停机段相交即拒绝；停机期间的补采请求会失败并保留窗口
            for down_start, down_end in self.downtime:
                if start < down_end and end > down_start:
                    raise ProtocolError(503, "source_down")
            now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
            # 来源只保留“当前时间之前”的数据，未来时间点没有读数
            step = dt.timedelta(seconds=self.tick_seconds)
            rows = []
            covered_to = start
            point = start
            while point < end:
                in_gap = any(g_start <= point < g_end for g_start, g_end in self.gaps)
                if in_gap or point >= now or len(rows) >= limit:
                    break
                rows.append(
                    {
                        "id": "r" + str(int(point.timestamp())),
                        "segment": args.get("segment", "segment1"),
                        "at": point.isoformat(),
                        "value": int(point.timestamp()) % 100000000,
                        "kind": "normal",
                    }
                )
                covered_to = point + step
                point += step
            complete = covered_to >= end
            coverage = item if complete else None
            return {"rows": rows, "coverage": coverage, "complete": complete}


def _fixed(at):
    value = int(at)
    return lambda: value


if __name__ == "__main__":
    main(CollectionProvider())
