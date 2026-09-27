"""课堂观察工作流：草稿保存、证据分次补充、审核完成后签发、变更全程留痕。"""
from dataclasses import dataclass,asdict,replace
from datetime import datetime,timezone,date
STATES=("draft","reviewing","reviewed","issued","expired")
TRANSITIONS={"draft":{"reviewing","expired"},"reviewing":{"reviewed","draft","expired"},"reviewed":{"issued","expired"},"issued":set(),"expired":set()}
EXPIRABLE=("draft","reviewing","reviewed")
class NotFoundError(KeyError): pass
def _utcnow(): return datetime.now(timezone.utc).isoformat()
def _check_date(value,name):
 try: date.fromisoformat(value)
 except (TypeError,ValueError): raise ValueError(f"{name} 必须是 YYYY-MM-DD 日期: {value!r}")
 return value
@dataclass(frozen=True)
class Observation:
 id:str; school:str; observed_on:str; valid_until:str
 state:str="draft"; version:int=1
 created_by:str=""; created_at:str=""; issued_by:str=""; issued_at:str=""
@dataclass(frozen=True)
class Evidence:
 id:str; observation_id:str; seq:int; content:str; added_by:str; added_at:str
@dataclass(frozen=True)
class AuditEvent:
 observation_id:str; seq:int; from_state:str; to_state:str; reason:str; actor:str; at:str; version:int
class Workflow:
 """观察记录的命令与查询入口。状态只经命令变更，查询不读系统时钟，因此跨日期边界重查结果一致。"""
 def __init__(self,store=None,clock=None):
  self.store=store; self.clock=clock or _utcnow
  self.observations={}; self.evidence={}; self.events={}; self.keys={}
  if store:
   data=store.latest()
   if data: self._load(data)
 # ---- 命令 ----
 def create(self,id,school,observed_on,valid_until,actor,key=None):
  if key and key in self.keys: return self.keys[key]
  if not id: raise ValueError("id 不能为空")
  if id in self.observations: raise ValueError(f"观察记录已存在: {id}")
  if not school: raise ValueError("学校不能为空")
  if not actor: raise ValueError("操作人不能为空")
  _check_date(observed_on,"observed_on"); _check_date(valid_until,"valid_until")
  if valid_until<observed_on: raise ValueError("有效期不能早于观察日期")
  obs=Observation(id,school,observed_on,valid_until,"draft",1,actor,self.clock())
  self.observations[id]=obs; self.evidence[id]=[]; self.events[id]=[]
  self._record(obs,"","draft",actor,"创建观察草稿",at=obs.created_at)
  return self._commit(key,asdict(obs))
 def add_evidence(self,obs_id,evidence_id,content,actor,key=None):
  if key and key in self.keys: return self.keys[key]
  obs=self._get(obs_id)
  if obs.state!="draft": raise ValueError(f"草稿状态才能补充证据，当前为 {obs.state}")
  if not content or not content.strip(): raise ValueError("证据内容不能为空")
  if any(e.id==evidence_id for e in self.evidence[obs_id]): raise ValueError(f"证据编号重复: {evidence_id}")
  ev=Evidence(evidence_id,obs_id,len(self.evidence[obs_id])+1,content.strip(),actor,self.clock())
  self.evidence[obs_id].append(ev)
  return self._commit(key,asdict(ev))
 def transition(self,obs_id,to_state,actor,reason,key=None,as_of=None):
  if key and key in self.keys: return self.keys[key]
  obs=self._get(obs_id)
  if not actor: raise ValueError("操作人不能为空")
  if not reason or not reason.strip(): raise ValueError("状态变更必须填写原因")
  if to_state not in TRANSITIONS[obs.state]: raise ValueError(f"不允许从 {obs.state} 变更为 {to_state}")
  if to_state=="expired":
   as_of=_check_date(as_of or self.clock()[:10],"as_of")
   if obs.valid_until>=as_of: raise ValueError(f"记录仍在有效期内（至 {obs.valid_until}），不能标记过期")
  now=self.clock()
  new=replace(obs,state=to_state,version=obs.version+1,issued_by=actor if to_state=="issued" else obs.issued_by,issued_at=now if to_state=="issued" else obs.issued_at)
  self.observations[obs_id]=new
  self._record(new,obs.state,to_state,actor,reason.strip(),at=now)
  return self._commit(key,asdict(new))
 def expire_due(self,as_of,actor,reason):
  _check_date(as_of,"as_of")
  due=[o for o in self.observations.values() if o.state in EXPIRABLE and o.valid_until<as_of]
  return [self.transition(o.id,"expired",actor,reason,as_of=as_of) for o in sorted(due,key=lambda o:o.id)]
 # ---- 查询 ----
 def search(self,school=None,date_from=None,date_to=None,state=None,include_expired=False):
  if state is not None and state not in STATES: raise ValueError(f"未知状态: {state}")
  if date_from: _check_date(date_from,"date_from")
  if date_to: _check_date(date_to,"date_to")
  rows=list(self.observations.values())
  if school is not None: rows=[o for o in rows if o.school==school]
  if date_from: rows=[o for o in rows if o.observed_on>=date_from]
  if date_to: rows=[o for o in rows if o.observed_on<=date_to]
  if state: rows=[o for o in rows if o.state==state]
  elif not include_expired: rows=[o for o in rows if o.state!="expired"]
  rows.sort(key=lambda o:(o.observed_on,o.id))
  return [self._view(o) for o in rows]
 def detail(self,obs_id):
  d=self._view(self._get(obs_id))
  d["history"]=[asdict(e) for e in self.events[obs_id]]
  return d
 # ---- 内部 ----
 def _get(self,obs_id):
  try: return self.observations[obs_id]
  except KeyError: raise NotFoundError(f"观察记录不存在: {obs_id}")
 def _view(self,o):
  d=asdict(o); d["evidence"]=[asdict(e) for e in self.evidence[o.id]]; return d
 def _record(self,obs,from_state,to_state,actor,reason,at=None):
  self.events[obs.id].append(AuditEvent(obs.id,len(self.events[obs.id])+1,from_state,to_state,reason,actor,at or self.clock(),obs.version))
 def _commit(self,key,result):
  if key: self.keys[key]=result
  if self.store: self.store.save(self._dump())
  return result
 def _dump(self):
  return {"observations":[asdict(self.observations[k]) for k in sorted(self.observations)],"evidence":{k:[asdict(e) for e in v] for k,v in sorted(self.evidence.items())},"events":{k:[asdict(e) for e in v] for k,v in sorted(self.events.items())},"keys":self.keys}
 def _load(self,data):
  self.observations={o["id"]:Observation(**o) for o in data["observations"]}
  self.evidence={k:[Evidence(**e) for e in v] for k,v in data.get("evidence",{}).items()}
  self.events={k:[AuditEvent(**e) for e in v] for k,v in data.get("events",{}).items()}
  self.keys=dict(data.get("keys",{}))
