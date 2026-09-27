"""JSON API 适配器：把 HTTP 形态的请求映射到课堂观察工作流。"""
from urllib.parse import urlparse,parse_qs
from .workflow import NotFoundError
def _arg(query,name):
 values=parse_qs(query).get(name)
 return values[0] if values else None
def dispatch(flow,method,path,body=None):
 body=body or {}
 url=urlparse(path)
 parts=[p for p in url.path.split("/") if p]
 try:
  if method=="POST" and parts==["observations"]: return 201,flow.create(body["id"],body["school"],body["observed_on"],body["valid_until"],body["actor"],body.get("idempotency_key"))
  if method=="POST" and parts==["observations","expire-due"]: return 200,flow.expire_due(body["as_of"],body["actor"],body["reason"])
  if method=="POST" and len(parts)==3 and parts[0]=="observations" and parts[2]=="evidence": return 201,flow.add_evidence(parts[1],body["id"],body["content"],body["actor"],body.get("idempotency_key"))
  if method=="POST" and len(parts)==3 and parts[0]=="observations" and parts[2]=="transition": return 200,flow.transition(parts[1],body["to_state"],body["actor"],body["reason"],body.get("idempotency_key"),body.get("as_of"))
  if method=="GET" and parts==["observations"]: return 200,flow.search(school=_arg(url.query,"school"),date_from=_arg(url.query,"date_from"),date_to=_arg(url.query,"date_to"),state=_arg(url.query,"state"),include_expired=_arg(url.query,"include_expired") in ("1","true","yes"))
  if method=="GET" and len(parts)==2 and parts[0]=="observations": return 200,flow.detail(parts[1])
 except NotFoundError as e: return 404,{"error":"not_found","detail":str(e)}
 except KeyError as e: return 400,{"error":"missing_field","field":str(e)}
 except ValueError as e: return 400,{"error":"invalid_request","detail":str(e)}
 return 404,{"error":"not_found"}
