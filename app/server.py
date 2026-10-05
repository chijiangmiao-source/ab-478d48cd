"""乱序轨迹复核器 HTTP 服务（仅依赖 Python 标准库）。

路由：
  GET  /                 复核页面
  GET  /healthz          健康检查
  GET  /api/sample       内置示例轨迹
  POST /api/analyze      提交 {instructions, events}，返回逐步状态与首个违规证据
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from simulator import InputError, analyze

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SAMPLE = {
    "rob_depth": 16,
    "instructions": [
        {"seq": 1, "op": "add", "dst": 1, "src1": 2, "src2": 3},
        {"seq": 2, "op": "add", "dst": 2, "src1": 1, "src2": 4},
        {"seq": 3, "op": "beq", "src1": 1, "src2": 2, "predicted": False},
        {"seq": 4, "op": "add", "dst": 3, "src1": 1},
        {"seq": 5, "op": "lw", "dst": 4, "src1": 3},
        {"seq": 6, "op": "add", "dst": 5, "src1": 4},
    ],
    "events": [
        {"type": "dispatch", "seq": 1},
        {"type": "dispatch", "seq": 2},
        {"type": "dispatch", "seq": 3},
        {"type": "dispatch", "seq": 4},
        {"type": "dispatch", "seq": 5},
        {"type": "dispatch", "seq": 6},
        {"type": "writeback", "seq": 1, "value": 10},
        {"type": "writeback", "seq": 2, "value": 20},
        {"type": "writeback", "seq": 4, "value": 40},
        {"type": "resolve", "seq": 3, "target": "taken"},
        {"type": "writeback", "seq": 6, "value": 60},
        {"type": "writeback", "seq": 3},
        {"type": "commit", "seq": 1},
        {"type": "commit", "seq": 2},
        {"type": "commit", "seq": 3},
    ],
}


class Handler(BaseHTTPRequestHandler):
    server_version = "OoOReviewer/1.0"

    def log_message(self, fmt, *args):  # 简洁日志
        print("[http] " + (fmt % args))

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel: str, ctype: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
        elif route == "/app.js":
            self._send_file("app.js", "application/javascript; charset=utf-8")
        elif route == "/styles.css":
            self._send_file("styles.css", "text/css; charset=utf-8")
        elif route in ("/healthz", "/health"):
            self._send_json({"status": "ok", "service": "ooo-trajectory-reviewer"})
        elif route == "/api/sample":
            self._send_json(SAMPLE)
        else:
            self._send_json({"error": "not found", "path": route}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route != "/api/analyze":
            self._send_json({"error": "not found", "path": route}, 404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._send_json({"error": "请求体为空"}, 400)
            return
        if length > 1_000_000:
            self._send_json({"error": "请求体过大（上限 1MB）"}, 413)
            return
        try:
            raw = self.rfile.read(length)
            trace = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"error": f"JSON 解析失败: {exc}"}, 400)
            return
        try:
            result = analyze(trace)
        except InputError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json(result, 200)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"乱序轨迹复核器监听 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
