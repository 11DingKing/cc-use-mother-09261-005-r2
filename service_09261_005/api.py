"""JSON API 适配器（与传输方式解耦）。

dispatch(flow, method, path, body, query) -> (status, dict)
路由：
  POST   /cases                          创建草稿
  POST   /cases/{id}/move                状态迁移（必须带 reason）
  POST   /cases/{id}/evidence            分次补充证据
  POST   /cases/{id}/evidence/{eid}/withdraw  撤回证据（软删除）
  GET    /cases/{id}                     单条详情（含证据与状态历史）
  GET    /cases?school=&observed_on=&state=&from=&to=
                                         按学校/日期/当前状态检索
"""

import json
from urllib.parse import urlsplit, parse_qs

from .errors import ConflictError, NotFoundError, ValidationError


def _first(query, name):
    values = query.get(name)
    return values[0] if values else None


def dispatch(flow, method, path, body=None, query=None):
    body = body or {}
    query = query or {}
    parts = [p for p in urlsplit(path).path.strip("/").split("/") if p]

    try:
        if method == "POST" and parts == ["cases"]:
            return 201, flow.create(
                body["id"], body["actor"],
                school=body.get("school"),
                observed_on=body.get("observed_on"),
                reason=body.get("reason"),
                idempotency_key=body.get("idempotency_key"),
            )

        if method == "POST" and len(parts) == 3 and parts[0] == "cases" \
                and parts[2] == "move":
            return 200, flow.move(
                parts[1], body["state"], body["actor"],
                reason=body.get("reason"),
                expected_version=body.get("expected_version"),
            )

        if method == "POST" and len(parts) == 3 and parts[0] == "cases" \
                and parts[2] == "evidence":
            status, result = 201, flow.add_evidence(
                parts[1], body["actor"], body["kind"], body["ref"],
                note=body.get("note", ""),
            )
            return status, result

        if method == "POST" and len(parts) == 5 and parts[0] == "cases" \
                and parts[2] == "evidence" and parts[4] == "withdraw":
            return 200, flow.withdraw_evidence(
                parts[1], parts[3], body["actor"],
                note=body.get("note", ""),
            )

        if method == "GET" and len(parts) == 2 and parts[0] == "cases":
            return 200, flow.get_case(parts[1])

        if method == "GET" and parts == ["cases"]:
            return 200, flow.query(
                school=_first(query, "school"),
                observed_on=_first(query, "observed_on"),
                state=_first(query, "state"),
                date_from=_first(query, "from"),
                date_to=_first(query, "to"),
            )

        return 404, {"error": "not_found", "path": path}

    except NotFoundError as exc:
        return 404, {"error": "not_found", "message": str(exc).strip("'")}
    except ConflictError as exc:
        return 409, {"error": "conflict", "message": str(exc)}
    except ValidationError as exc:
        return 400, {"error": "validation", "message": str(exc)}
    except KeyError as exc:
        return 400, {"error": "validation", "message": "缺少字段: %s" % str(exc).strip("'")}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "validation", "message": str(exc)}


def dispatch_url(flow, method, url, body=None):
    """供测试与简单客户端使用：直接传完整 URL。"""
    parsed = urlsplit(url)
    query = {k: v for k, v in parse_qs(parsed.query).items()}
    return dispatch(flow, method, parsed.path, body=body, query=query)


def dumps(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
