#!/usr/bin/env python3
"""API/HTTP 冒烟测试：针对运行中的服务验证健康响应、页面与核心判定。

用法：python3 scripts/smoke.py [BASE_URL]
重点覆盖：误预测分支回滚轨迹判定一致且拒绝迟到写回；
精确异常边界冲刷正确；内置违规轨迹必须被定位。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8080"
failures = []


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r, timeout=10) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


try:
    status, body, _ = req("GET", "/healthz")
    health = json.loads(body)
    check("GET /healthz 返回 200 且 status=ok",
          status == 200 and health.get("status") == "ok", f"status={status}")

    status, body, headers = req("GET", "/")
    check("GET / 返回 HTML 页面",
          status == 200 and b"<!DOCTYPE html>" in body
          and "text/html" in headers.get("Content-Type", ""))

    status, body, _ = req("GET", "/api/sample")
    check("GET /api/sample 返回示例轨迹", status == 200 and b"events" in body)

    # 1) 误预测分支回滚：必须一致，且迟到写回被拒绝，错误标签不提交
    rollback = {
        "instructions": [
            {"seq": 1, "op": "add", "dst": 1, "src1": 2},
            {"seq": 2, "op": "beq", "src1": 1, "src2": 2, "predicted": False},
            {"seq": 3, "op": "add", "dst": 3, "src1": 1},
            {"seq": 4, "op": "add", "dst": 4, "src1": 3},
        ],
        "events": [
            {"type": "dispatch", "seq": 1},
            {"type": "dispatch", "seq": 2},
            {"type": "dispatch", "seq": 3},
            {"type": "dispatch", "seq": 4},
            {"type": "writeback", "seq": 1, "value": 10},
            {"type": "writeback", "seq": 3, "value": 30},
            {"type": "resolve", "seq": 2, "target": "taken"},
            {"type": "writeback", "seq": 4, "value": 40},
            {"type": "writeback", "seq": 2},
            {"type": "commit", "seq": 1},
            {"type": "commit", "seq": 2},
        ],
    }
    status, body, _ = req("POST", "/api/analyze", rollback)
    r1 = json.loads(body)
    check("分支回滚轨迹判定 consistent", status == 200 and r1.get("verdict") == "consistent")
    refused = [e for e in r1.get("events", []) if e.get("outcome") == "refused"]
    check("被清除指令 seq=4 的迟到写回被拒绝",
          any(e["detail"].get("seq") == 4 for e in refused))
    committed_tags = set(r1["final_state"]["committed_map"].values())
    check("错误路径标签 p9/p10 未进入已提交映射",
          not (committed_tags & {9, 10}), f"committed={committed_tags}")
    check("逐步事件均含 before/after 证据",
          all("before" in e and "after" in e for e in r1.get("events", [])))

    # 2) 精确异常边界：队首异常冲刷自身与更年轻，映射恢复
    exception = {
        "instructions": [
            {"seq": 1, "op": "add", "dst": 1, "src1": 0},
            {"seq": 2, "op": "lw", "dst": 2, "src1": 1},
            {"seq": 3, "op": "add", "dst": 3},
        ],
        "events": [
            {"type": "dispatch", "seq": 1},
            {"type": "writeback", "seq": 1, "value": 11},
            {"type": "commit", "seq": 1},
            {"type": "dispatch", "seq": 2},
            {"type": "dispatch", "seq": 3},
            {"type": "writeback", "seq": 3, "value": 33},
            {"type": "exception", "seq": 2, "cause": "page fault"},
        ],
    }
    status, body, _ = req("POST", "/api/analyze", exception)
    r2 = json.loads(body)
    check("异常边界轨迹判定 consistent", status == 200 and r2.get("verdict") == "consistent")
    exc = r2["events"][-1]
    check("异常冲刷自身与全部更年轻指令",
          exc["detail"].get("flushed") == [2, 3], str(exc["detail"].get("flushed")))
    check("异常后映射恢复至精确状态 r2->p2, r1->p8",
          r2["final_state"]["map_table"].get("2") == 2
          and r2["final_state"]["map_table"].get("1") == 8)

    # 3) 违规轨迹：异常未到队首必须被定位
    bad = json.loads(json.dumps(exception))
    bad["events"][-1] = {"type": "exception", "seq": 3, "cause": "fault"}
    status, body, _ = req("POST", "/api/analyze", bad)
    r3 = json.loads(body)
    check("异常未到队首被判定 violation",
          r3.get("verdict") == "violation"
          and r3["first_violation"]["code"] == "exception_not_at_head")
    check("首个违规携带前后证据",
          bool(r3.get("first_violation", {}).get("evidence"))
          and "before" in r3["first_violation"] and "after" in r3["first_violation"])

    # 4) 输入错误返回 400
    try:
        req("POST", "/api/analyze", {"instructions": [], "events": []})
        check("空输入返回 400", False)
    except urllib.error.HTTPError as e:
        check("空输入返回 400", e.code == 400)

except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] 冒烟过程异常: {exc!r}")
    failures.append("exception")

print("\n冒烟结果：" + ("全部通过 ✅" if not failures else f"{len(failures)} 项失败 ❀ {failures}"))
sys.exit(1 if failures else 0)
