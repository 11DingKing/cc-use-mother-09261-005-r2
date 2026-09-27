import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from service_09261_005.workflow import Workflow
from service_09261_005.store import SQLiteStore
from service_09261_005.api import dispatch, dispatch_url
from service_09261_005.errors import ConflictError, NotFoundError, ValidationError

TZ = timezone(timedelta(hours=8))


class MutableClock:
    """可手动推进的业务时钟，用于模拟跨日期边界。"""

    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def advance(self, **kw):
        self.dt += timedelta(**kw)


def make_flow(clock=None):
    store = SQLiteStore(":memory:", now=clock or (lambda: datetime.now(TZ)))
    return Workflow(store), store


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        self.flow, self.store = make_flow(self.clock)

    def test_create_draft_and_idempotency(self):
        c = self.flow.create("c1", "张编辑", school="育才中学",
                             observed_on="2026-09-26", idempotency_key="k1")
        self.assertEqual(c["state"], "draft")
        self.assertEqual(c["version"], 1)
        self.assertEqual(c["active_evidence_count"], 0)
        # 相同幂等键重放：返回同一结果，不产生第二条
        again = self.flow.create("c1", "张编辑", school="育才中学",
                                 observed_on="2026-09-26", idempotency_key="k1")
        self.assertEqual(again["id"], "c1")
        self.assertEqual(len(self.flow.snapshot()), 1)
        # 幂等键换请求体 → 冲突
        with self.assertRaises(ConflictError):
            self.flow.create("c2", "张编辑", school="其他中学",
                             observed_on="2026-09-26", idempotency_key="k1")

    def test_duplicate_id_rejected(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        with self.assertRaises(ConflictError):
            self.flow.create("c1", "a", school="s", observed_on="2026-09-26")

    def test_invalid_date_rejected(self):
        with self.assertRaises(ValidationError):
            self.flow.create("c9", "a", school="s", observed_on="2026/09/26")

    def test_evidence_incremental_before_issue(self):
        self.flow.create("c1", "张编辑", school="育才中学",
                         observed_on="2026-09-26")
        self.flow.add_evidence("c1", "张编辑", "照片", "p/001.jpg", "课堂全景")
        self.clock.advance(days=1)  # 分次补充，跨了一天
        c = self.flow.add_evidence("c1", "李老师", "记录单", "f/002.pdf")
        self.assertEqual(c["active_evidence_count"], 2)
        self.assertEqual(c["evidence"][0]["added_at"][:10], "2026-09-26")
        self.assertEqual(c["evidence"][1]["added_at"][:10], "2026-09-27")

    def test_cannot_issue_without_review_or_evidence(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        # 草稿不能直接签发
        with self.assertRaises(ConflictError):
            self.flow.move("c1", "issued", "审核员", reason="误操作签发")
        self.flow.move("c1", "reviewing", "张编辑", reason="证据齐了，送审")
        # 审核中但无证据也不能签发
        with self.assertRaises(ConflictError):
            self.flow.move("c1", "issued", "王审核", reason="批准")

    def test_full_review_and_issue_freezes(self):
        self.flow.create("c1", "张编辑", school="育才中学",
                         observed_on="2026-09-26", reason="首次听课")
        self.flow.add_evidence("c1", "张编辑", "照片", "p/001.jpg")
        self.flow.add_evidence("c1", "李老师", "记录单", "f/002.pdf", "批注版")
        reviewing = self.flow.move("c1", "reviewing", "张编辑", reason="送审")
        self.assertEqual(reviewing["version"], 2)
        issued = self.flow.move("c1", "issued", "王审核",
                                reason="材料完整，同意签发",
                                expected_version=2)
        self.assertEqual(issued["state"], "issued")
        self.assertEqual(issued["version"], 3)
        snap = issued["frozen_snapshot"]
        self.assertEqual(snap["state"], "issued")
        self.assertEqual(len(snap["evidence"]), 2)
        # 每次状态变化都能看出原因
        reasons = [(t["from_state"], t["to_state"], t["reason"])
                   for t in issued["transitions"]]
        self.assertEqual(reasons, [
            (None, "draft", "首次听课"),
            ("draft", "reviewing", "送审"),
            ("reviewing", "issued", "材料完整，同意签发"),
        ])
        # 签发后证据关系冻结
        with self.assertRaises(ConflictError):
            self.flow.add_evidence("c1", "x", "照片", "p/003.jpg")
        with self.assertRaises(ConflictError):
            self.flow.withdraw_evidence("c1", 1, "x", note="想撤")
        # 签发是终态
        with self.assertRaises(ConflictError):
            self.flow.move("c1", "draft", "x", reason="打回")

    def test_return_to_draft_and_reissue(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        self.flow.add_evidence("c1", "a", "照片", "p/1.jpg")
        self.flow.move("c1", "reviewing", "a", reason="送审")
        self.flow.move("c1", "draft", "王审核", reason="照片模糊，退回补拍")
        c = self.flow.get_case("c1")
        self.assertEqual(c["state"], "draft")
        self.flow.add_evidence("c1", "a", "照片", "p/2.jpg", "重拍")
        self.flow.move("c1", "reviewing", "a", reason="补拍后重新送审",
                       expected_version=3)
        issued = self.flow.move("c1", "issued", "王审核", reason="通过")
        refs = {e["ref"] for e in issued["frozen_snapshot"]["evidence"]}
        self.assertEqual(refs, {"p/1.jpg", "p/2.jpg"})

    def test_withdraw_evidence_keeps_history(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        self.flow.add_evidence("c1", "a", "照片", "p/1.jpg")
        e2 = self.flow.add_evidence("c1", "a", "记录单", "f/1.pdf")
        self.flow.withdraw_evidence("c1", e2["added_evidence_id"], "a",
                                    note="传错文件")
        c = self.flow.get_case("c1")
        self.assertEqual(c["active_evidence_count"], 1)
        self.assertIsNotNone(c["evidence"][1]["superseded_at"])
        # 行没有被删除
        self.assertEqual(len(c["evidence"]), 2)
        # 重复撤回报错
        with self.assertRaises(ConflictError):
            self.flow.withdraw_evidence("c1", e2["added_evidence_id"], "a")
        # 撤回后没有有效证据则不能送审后签发
        self.flow.withdraw_evidence("c1", 1, "a", note="唯一一条也撤")
        self.flow.move("c1", "reviewing", "a", reason="先送审试试")
        with self.assertRaises(ConflictError):
            self.flow.move("c1", "issued", "王审核", reason="批准")

    def test_cancel_is_terminal(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        self.flow.move("c1", "cancelled", "a", reason="重复录入，取消")
        with self.assertRaises(ConflictError):
            self.flow.move("c1", "reviewing", "a", reason="恢复")
        with self.assertRaises(ConflictError):
            self.flow.add_evidence("c1", "a", "照片", "p/1.jpg")

    def test_stale_version_conflict(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        self.flow.add_evidence("c1", "a", "照片", "p/1.jpg")
        self.flow.move("c1", "reviewing", "a", reason="送审")  # 版本到 2
        # 仍按 v1 提交签发 → 冲突，状态不变
        with self.assertRaises(ConflictError):
            self.flow.move("c1", "issued", "王审核", reason="批准",
                           expected_version=1)
        self.assertEqual(self.flow.get_case("c1")["state"], "reviewing")

    def test_get_unknown_case(self):
        with self.assertRaises(NotFoundError):
            self.flow.get_case("nope")

    def test_move_requires_reason(self):
        self.flow.create("c1", "a", school="s", observed_on="2026-09-26")
        with self.assertRaises(ValidationError):
            self.flow.move("c1", "reviewing", "a", reason="   ")


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        self.flow, _ = make_flow(self.clock)
        # 两所学校、多个日期，构造不同状态
        self.flow.create("c1", "a", school="育才中学", observed_on="2026-09-26")
        self.flow.create("c2", "a", school="育才中学", observed_on="2026-09-27")
        self.flow.create("c3", "a", school="实验一小", observed_on="2026-09-27")
        for cid in ("c1", "c2", "c3"):
            self.flow.add_evidence(cid, "a", "照片", "p/%s.jpg" % cid)
        # c1 走到签发；c2 审核中；c3 留草稿
        self.flow.move("c1", "reviewing", "a", reason="送审")
        self.flow.move("c1", "issued", "王审核", reason="通过")
        self.flow.move("c2", "reviewing", "a", reason="送审")

    def test_filter_by_school(self):
        rows = self.flow.query(school="育才中学")
        self.assertEqual([r["id"] for r in rows], ["c1", "c2"])

    def test_filter_by_date(self):
        rows = self.flow.query(observed_on="2026-09-27")
        self.assertEqual({r["id"] for r in rows}, {"c2", "c3"})

    def test_filter_by_current_state_excludes_old(self):
        # c1 曾处于 draft/reviewing，但当前是 issued：
        # 按草稿/审核中过滤时绝不能把它混进来
        drafts = self.flow.query(state="draft")
        self.assertEqual([r["id"] for r in drafts], ["c3"])
        reviewing = self.flow.query(state="reviewing")
        self.assertEqual([r["id"] for r in reviewing], ["c2"])
        issued = self.flow.query(state="issued")
        self.assertEqual([r["id"] for r in issued], ["c1"])

    def test_combined_filter_and_range(self):
        rows = self.flow.query(school="育才中学", state="issued",
                               date_from="2026-09-01", date_to="2026-09-30")
        self.assertEqual([r["id"] for r in rows], ["c1"])
        none = self.flow.query(school="育才中学", state="draft")
        self.assertEqual(none, [])

    def test_state_then_requery_stays_consistent_across_date_boundary(self):
        """核心诉求：跨日期边界重新查询同一批观察，签发状态与证据关系一致。"""
        before = {r["id"]: r for r in self.flow.query(school="育才中学")}
        issued_before = self.flow.get_case("c1")
        frozen_refs_before = {e["ref"]
                              for e in issued_before["frozen_snapshot"]["evidence"]}

        # 越过国庆假期，跨学期、跨日期边界后重新查询
        self.clock.advance(days=10)
        after = {r["id"]: r for r in self.flow.query(school="育才中学")}
        self.assertEqual({k: v["state"] for k, v in before.items()},
                         {k: v["state"] for k, v in after.items()})
        issued_after = self.flow.get_case("c1")
        self.assertEqual(issued_after["state"], "issued")
        self.assertEqual(issued_after["version"], 3)
        frozen_refs_after = {e["ref"]
                             for e in issued_after["frozen_snapshot"]["evidence"]}
        self.assertEqual(frozen_refs_before, frozen_refs_after)
        # 当前证据明细与签发快照仍然一致
        live_refs = {e["ref"] for e in issued_after["evidence"]
                     if e["superseded_at"] is None}
        self.assertEqual(live_refs, frozen_refs_after)
        # 按当前状态检索，结果集不因换了一天而变化
        self.assertEqual(
            [r["id"] for r in self.flow.query(state="issued")], ["c1"])


class TestPersistence(unittest.TestCase):
    def test_reopen_db_keeps_state_and_freeze(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            clock = MutableClock(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
            store = SQLiteStore(path, now=clock)
            flow = Workflow(store)
            flow.create("c1", "a", school="育才中学", observed_on="2026-09-26")
            flow.add_evidence("c1", "a", "照片", "p/1.jpg")
            flow.move("c1", "reviewing", "a", reason="送审")
            flow.move("c1", "issued", "王审核", reason="通过")
            store.close()

            # 模拟假期后重新打开服务（甚至换了进程）
            clock.advance(days=15)
            store2 = SQLiteStore(path, now=clock)
            flow2 = Workflow(store2)
            c = flow2.get_case("c1")
            self.assertEqual(c["state"], "issued")
            self.assertEqual(len(c["frozen_snapshot"]["evidence"]), 1)
            self.assertEqual(len(flow2.query(state="draft")), 0)
            self.assertEqual(len(flow2.query(state="issued")), 1)
            with self.assertRaises(ConflictError):
                flow2.add_evidence("c1", "a", "照片", "p/2.jpg")
            store2.close()
        finally:
            os.unlink(path)


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        self.store = SQLiteStore(":memory:", now=self.clock)
        self.flow = Workflow(self.store)

    def test_api_full_flow_status_codes(self):
        status, c = dispatch(self.flow, "POST", "/cases", {
            "id": "c1", "actor": "张编辑", "school": "育才中学",
            "observed_on": "2026-09-26", "reason": "首次听课",
            "idempotency_key": "k1",
        })
        self.assertEqual(status, 201)
        # 幂等重放
        status, again = dispatch_url(
            self.flow, "POST",
            "/cases?id=ignored",
            {"id": "c1", "actor": "张编辑", "school": "育才中学",
             "observed_on": "2026-09-26", "idempotency_key": "k1"})
        self.assertEqual(status, 201)
        self.assertEqual(again["id"], "c1")

        status, _ = dispatch(self.flow, "POST", "/cases/c1/evidence",
                             {"actor": "张编辑", "kind": "照片",
                              "ref": "p/1.jpg"})
        self.assertEqual(status, 201)

        status, _ = dispatch(self.flow, "POST", "/cases/c1/move",
                             {"actor": "张编辑", "state": "reviewing",
                              "reason": "送审"})
        self.assertEqual(status, 200)

        status, body = dispatch(self.flow, "POST", "/cases/c1/move",
                                {"actor": "王审核", "state": "issued",
                                 "reason": "通过", "expected_version": 2})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "issued")

        # 签发后补证据 → 409
        status, err = dispatch(self.flow, "POST", "/cases/c1/evidence",
                               {"actor": "x", "kind": "照片", "ref": "p/2.jpg"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")

    def test_api_validation_and_404(self):
        status, err = dispatch(self.flow, "POST", "/cases",
                               {"id": "c1", "actor": "a"})
        self.assertEqual(status, 400)

        status, err = dispatch(self.flow, "GET", "/cases/missing")
        self.assertEqual(status, 404)

        status, err = dispatch(self.flow, "GET", "/nope")
        self.assertEqual(status, 404)

    def test_api_query_filters(self):
        dispatch(self.flow, "POST", "/cases",
                 {"id": "c1", "actor": "a", "school": "育才中学",
                  "observed_on": "2026-09-26"})
        status, rows = dispatch_url(
            self.flow, "GET",
            "/cases?school=%E8%82%B2%E6%89%8D%E4%B8%AD%E5%AD%A6&state=draft")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in rows], ["c1"])
        status, rows = dispatch_url(self.flow, "GET", "/cases?state=issued")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
