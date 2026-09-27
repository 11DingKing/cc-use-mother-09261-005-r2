import unittest
from urllib.parse import urlencode
from service_09261_005.workflow import Workflow,NotFoundError
from service_09261_005.store import SQLiteStore
from service_09261_005.api import dispatch
CLOCK="2026-09-27T08:00:00+00:00"
def new_flow(**kw):
 f=Workflow(clock=lambda:CLOCK,**kw)
 f.create("o1","阳光小学","2026-09-10","2026-09-30","ed")
 f.create("o2","阳光小学","2026-09-12","2026-09-30","ed")
 f.create("o3","育才中学","2026-09-15","2026-10-15","ed")
 return f
def to_reviewed(f,oid):
 f.transition(oid,"reviewing","ed","提交审核")
 f.transition(oid,"reviewed","rev","审核通过")
class TestLifecycle(unittest.TestCase):
 def test_draft_first_and_batched_evidence(self):
  f=new_flow()
  self.assertEqual(f.detail("o1")["state"],"draft")
  f.add_evidence("o1","e1","第一次听课记录","ed")
  f.add_evidence("o1","e2","学生作业抽样","ed")
  f.add_evidence("o1","e3","访谈纪要","ed")
  ev=f.detail("o1")["evidence"]
  self.assertEqual([e["seq"] for e in ev],[1,2,3])
  self.assertEqual([e["id"] for e in ev],["e1","e2","e3"])
 def test_evidence_only_in_draft(self):
  f=new_flow()
  f.transition("o1","reviewing","ed","提交审核")
  with self.assertRaises(ValueError): f.add_evidence("o1","e1","补材料","ed")
  f.transition("o1","draft","rev","证据不足，退回补充")
  f.add_evidence("o1","e1","补材料","ed")
  self.assertEqual(len(f.detail("o1")["evidence"]),1)
 def test_issue_requires_completed_review(self):
  f=new_flow()
  with self.assertRaises(ValueError): f.transition("o1","issued","ed","直接签发")
  f.transition("o1","reviewing","ed","提交审核")
  with self.assertRaises(ValueError): f.transition("o1","issued","ed","审核未完就签发")
  f.transition("o1","reviewed","rev","审核通过")
  done=f.transition("o1","issued","ed","签发试用结论")
  self.assertEqual(done["state"],"issued")
  self.assertEqual(done["issued_by"],"ed")
  self.assertEqual(done["issued_at"],CLOCK)
 def test_every_change_has_reason(self):
  f=new_flow()
  with self.assertRaises(ValueError): f.transition("o1","reviewing","ed","")
  with self.assertRaises(ValueError): f.transition("o1","reviewing","ed","   ")
  f.transition("o1","reviewing","ed","提交审核")
  f.transition("o1","draft","rev","退回补充证据")
  f.transition("o1","reviewing","ed","再次提交")
  to_reviewed_called=f.transition("o1","reviewed","rev","审核通过")
  f.transition("o1","issued","ed","签发结论")
  history=f.detail("o1")["history"]
  self.assertEqual([h["to_state"] for h in history],["draft","reviewing","draft","reviewing","reviewed","issued"])
  self.assertEqual([h["from_state"] for h in history],["","draft","reviewing","draft","reviewing","reviewed"])
  self.assertTrue(all(h["reason"] for h in history))
  self.assertEqual(history[-1]["reason"],"签发结论")
  self.assertEqual(history[-1]["version"],to_reviewed_called["version"]+1)
class TestSearch(unittest.TestCase):
 def setUp(self):
  self.f=new_flow()
  to_reviewed(self.f,"o1"); self.f.transition("o1","issued","ed","签发")
  self.f.expire_due("2026-10-01","sys","观察期结束，跨学期未审完")
 def test_default_excludes_expired(self):
  self.assertEqual([r["id"] for r in self.f.search(school="阳光小学")],["o1"])
  self.assertEqual([r["id"] for r in self.f.search()],["o1","o3"])
 def test_explicit_expired_and_include(self):
  self.assertEqual([r["id"] for r in self.f.search(state="expired")],["o2"])
  self.assertEqual([r["id"] for r in self.f.search(school="阳光小学",include_expired=True)],["o1","o2"])
 def test_filters_by_date_and_state(self):
  self.assertEqual([r["id"] for r in self.f.search(date_from="2026-09-11",date_to="2026-09-20",include_expired=True)],["o2","o3"])
  self.assertEqual([r["id"] for r in self.f.search(state="issued")],["o1"])
  self.assertEqual([r["id"] for r in self.f.search(school="育才中学",state="draft")],["o3"])
  with self.assertRaises(ValueError): self.f.search(state="bogus")
class TestExpiry(unittest.TestCase):
 def test_expire_only_after_valid_until(self):
  f=new_flow()
  self.assertEqual(f.expire_due("2026-09-30","sys","到期检查"),[])
  due=f.expire_due("2026-10-01","sys","国庆假期后观察期结束")
  self.assertEqual([d["id"] for d in due],["o1","o2"])
  self.assertEqual(f.detail("o3")["state"],"draft")
  self.assertEqual(f.detail("o1")["history"][-1]["reason"],"国庆假期后观察期结束")
 def test_issued_never_expires(self):
  f=new_flow()
  to_reviewed(f,"o1"); f.transition("o1","issued","ed","签发")
  due=f.expire_due("2027-02-01","sys","跨学期清理")
  self.assertEqual([d["id"] for d in due],["o2","o3"])
  self.assertEqual(f.detail("o1")["state"],"issued")
 def test_manual_expire_requires_past_valid_until(self):
  f=new_flow()
  with self.assertRaises(ValueError): f.transition("o1","expired","sys","提前作废",as_of="2026-09-30")
  ok=f.transition("o1","expired","sys","学校提前结束试用",as_of="2026-10-05")
  self.assertEqual(ok["state"],"expired")
class TestConsistency(unittest.TestCase):
 def test_cross_date_boundary_requery_is_stable(self):
  store=SQLiteStore()
  f=Workflow(store,clock=lambda:"2026-09-27T08:00:00+00:00")
  f.create("o1","阳光小学","2026-09-10","2026-09-30","ed")
  f.add_evidence("o1","e1","听课记录","ed")
  f.add_evidence("o1","e2","作业抽样","ed")
  to_reviewed(f,"o1"); f.transition("o1","issued","ed","签发结论")
  before=f.search(school="阳光小学"); before_detail=f.detail("o1")
  later=Workflow(store,clock=lambda:"2027-02-20T08:00:00+00:00")
  self.assertEqual(later.search(school="阳光小学"),before)
  self.assertEqual(later.detail("o1"),before_detail)
  issued=later.search(state="issued")
  self.assertEqual([r["id"] for r in issued],["o1"])
  self.assertEqual([e["id"] for e in issued[0]["evidence"]],["e1","e2"])
class TestIdempotency(unittest.TestCase):
 def test_replayed_keys_do_not_duplicate(self):
  f=new_flow()
  a=f.create("o9","实验小学","2026-09-01","2026-09-20","ed",key="k-create")
  b=f.create("o9","实验小学","2026-09-01","2026-09-20","ed",key="k-create")
  self.assertEqual(a,b)
  self.assertEqual(len([o for o in f.search(include_expired=True) if o["id"]=="o9"]),1)
  f.add_evidence("o9","e1","材料","ed",key="k-ev")
  f.add_evidence("o9","e1","材料","ed",key="k-ev")
  self.assertEqual(len(f.detail("o9")["evidence"]),1)
  t1=f.transition("o9","reviewing","ed","提交",key="k-tr")
  t2=f.transition("o9","reviewing","ed","提交",key="k-tr")
  self.assertEqual(t1,t2)
  self.assertEqual(f.detail("o9")["version"],2)
  self.assertEqual(len(f.detail("o9")["history"]),2)
class TestValidation(unittest.TestCase):
 def test_create_validation(self):
  f=new_flow()
  with self.assertRaises(ValueError): f.create("o1","某校","2026-09-01","2026-09-30","ed")
  with self.assertRaises(ValueError): f.create("o8","","2026-09-01","2026-09-30","ed")
  with self.assertRaises(ValueError): f.create("o8","某校","2026-09-01","2026-08-01","ed")
  with self.assertRaises(ValueError): f.create("o8","某校","not-a-date","2026-09-30","ed")
 def test_unknown_observation(self):
  f=new_flow()
  with self.assertRaises(NotFoundError): f.detail("nope")
  with self.assertRaises(NotFoundError): f.transition("nope","reviewing","ed","提交")
  with self.assertRaises(NotFoundError): f.add_evidence("nope","e1","x","ed")
 def test_duplicate_evidence_id(self):
  f=new_flow()
  f.add_evidence("o1","e1","a","ed")
  with self.assertRaises(ValueError): f.add_evidence("o1","e1","b","ed")
class TestApi(unittest.TestCase):
 def setUp(self): self.f=Workflow(clock=lambda:CLOCK)
 def test_http_flow(self):
  s,b=dispatch(self.f,"POST","/observations",{"id":"o1","school":"阳光小学","observed_on":"2026-09-10","valid_until":"2026-09-30","actor":"ed"})
  self.assertEqual((s,b["state"]),(201,"draft"))
  s,b=dispatch(self.f,"POST","/observations/o1/evidence",{"id":"e1","content":"听课记录","actor":"ed"})
  self.assertEqual((s,b["seq"]),(201,1))
  s,b=dispatch(self.f,"POST","/observations/o1/transition",{"to_state":"issued","actor":"ed","reason":"跳过审核"})
  self.assertEqual(s,400)
  for st,reason in (("reviewing","提交"),("reviewed","通过"),("issued","签发")):
   s,b=dispatch(self.f,"POST","/observations/o1/transition",{"to_state":st,"actor":"ed","reason":reason})
   self.assertEqual(s,200)
  s,b=dispatch(self.f,"GET","/observations?"+urlencode({"school":"阳光小学","state":"issued"}))
  self.assertEqual((s,[r["id"] for r in b]),(200,["o1"]))
  s,b=dispatch(self.f,"GET","/observations/o1")
  self.assertEqual(s,200)
  self.assertEqual(len(b["history"]),4)
  self.assertEqual(b["evidence"][0]["content"],"听课记录")
 def test_api_expiry_and_default_filter(self):
  dispatch(self.f,"POST","/observations",{"id":"o1","school":"阳光小学","observed_on":"2026-09-10","valid_until":"2026-09-30","actor":"ed"})
  s,b=dispatch(self.f,"POST","/observations/expire-due",{"as_of":"2026-10-08","actor":"sys","reason":"国庆假期后观察期结束"})
  self.assertEqual((s,[d["id"] for d in b]),(200,["o1"]))
  s,b=dispatch(self.f,"GET","/observations")
  self.assertEqual(b,[])
  s,b=dispatch(self.f,"GET","/observations?state=expired")
  self.assertEqual([r["id"] for r in b],["o1"])
 def test_api_errors(self):
  s,b=dispatch(self.f,"GET","/observations/nope")
  self.assertEqual((s,b["error"]),(404,"not_found"))
  s,b=dispatch(self.f,"POST","/observations",{"id":"o1"})
  self.assertEqual((s,b["error"]),(400,"missing_field"))
  s,b=dispatch(self.f,"GET","/elsewhere")
  self.assertEqual(s,404)
if __name__=="__main__": unittest.main()
