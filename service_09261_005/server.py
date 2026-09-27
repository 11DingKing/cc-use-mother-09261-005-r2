"""本地 HTTP 服务入口（仅依赖标准库）。

启动：
    python3 -m service_09261_005.server --db ./observations.db --port 8000
    python3 -m service_09261_005.server --now 2026-09-27T10:00:00+08:00
        # --now 可固定业务时钟，便于跨日期边界做可重现查询/演示

数据落在单个 SQLite 文件，适合编辑本机使用。
"""

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

from .api import dispatch, dumps
from .store import SQLiteStore
from .workflow import Workflow


def make_clock(fixed=None):
    if fixed:
        dt = datetime.fromisoformat(fixed)

        def clock():
            return dt
        return clock

    from .store import utcnow
    return utcnow


def build_app(db_path, fixed_now=None):
    store = SQLiteStore(db_path, now=make_clock(fixed_now))
    flow = Workflow(store)
    return store, flow


class Handler(BaseHTTPRequestHandler):
    server_version = "ObservationService/1.0"

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _handle(self, method):
        parsed = urlsplit(self.path)
        if method == "GET":
            body = {}
        else:
            body = self._read_body()
            if body is None and method != "GET":
                self._write(400, {"error": "validation",
                                  "message": "请求体必须是 JSON 对象"})
                return
        query = parse_qs(parsed.query)
        status, payload = dispatch(
            self.server.flow, method, parsed.path, body=body, query=query
        )
        self._write(status, payload)

    def _write(self, status, payload):
        data = dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        # 简化本地日志
        print("%s - %s" % (self.address_string(), fmt % args))


def main(argv=None):
    parser = argparse.ArgumentParser(description="教材试用观察本地服务")
    parser.add_argument("--db", default="observations.db", help="SQLite 文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--now", default=None,
                        help="固定业务时钟（ISO-8601），用于演示/测试")
    args = parser.parse_args(argv)

    store, flow = build_app(args.db, fixed_now=args.now)
    # 单线程服务器：SQLite 连接不跨线程，本地单用户串行处理即可
    httpd = HTTPServer((args.host, args.port), Handler)
    httpd.flow = flow
    httpd.store = store
    print("观察记录服务已启动: http://%s:%d  (db=%s)"
          % (args.host, args.port, args.db))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
