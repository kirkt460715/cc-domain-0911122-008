"""采集调度门面：服务层只依赖这一个入口。"""

from .plans import Plans
from .engine import CollectionEngine
from .runtime import Clock, SourceClient


class Scheduler:
    def __init__(self, store, readings, clock=None, client=None):
        self.store = store
        self.plans = Plans(store)
        self.clock = clock or Clock()
        self.client = client or SourceClient()
        self.engine = CollectionEngine(
            store, self.plans, self.clock, self.client, readings
        )

    def now(self):
        return self.clock()

    # 计划生命周期
    def create_plan(self, plan_id, meter, definition, now=None):
        return self.plans.create(
            plan_id, meter, definition, self.clock() if now is None else now
        )

    def get_plan(self, plan_id, revision=None):
        return self.plans.get(plan_id, revision)

    def list_plans(self):
        return self.plans.list()

    def plan_versions(self, plan_id):
        return self.plans.list_versions(plan_id)

    # 日历与窗口
    def preview(self, plan_id, first_day, last_day, revision=None):
        return self.engine.preview(plan_id, first_day, last_day, revision)

    def materialize(self, plan_id, first_day, last_day):
        return self.engine.materialize(plan_id, first_day, last_day)

    def windows(self, plan_id=None, status=None, limit=500):
        return self.engine.list_windows(plan_id, status, limit)

    def window(self, window_id):
        return self.engine.get_window(window_id)

    def notes(self, plan_id, business_date=None):
        return self.engine.notes(plan_id, business_date)

    # 执行
    def tick(self, max_runs=5, max_readings=100, now=None):
        return self.engine.tick(max_runs, max_readings, now)

    def backfill(self, plan_id, first_day=None, last_day=None, mode="all",
                 max_runs=10, max_readings=100, now=None):
        return self.engine.backfill(
            plan_id, first_day, last_day, mode, max_runs, max_readings, now
        )

    def abandon_gap(self, window_id, reason, now=None):
        return self.engine.mark_gap_abandoned(window_id, reason, now)

    def runs(self, window_id=None):
        return self.engine.runs(window_id)

    def coverage(self, plan_id, first_day=None, last_day=None):
        return self.engine.coverage_report(plan_id, first_day, last_day)
