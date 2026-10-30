"""Web 控制台端到端冒烟测试（需先启动 python -m liveplugin.web --port 8765）。

    python tools/test_web_http.py [base_url]
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


def post(path, body=None):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    ok = True

    s = get("/api/state")
    assert s["app_connected"], "app 未连接"
    assert s["frames_sent"] > 0, "没有帧在推送"
    print("state OK:", s["mode"], "| frames:", s["frames_sent"])

    # 手动注入全类型事件
    for ev in [{"type": "comment", "nickname": "小测", "content": "你好呀，控制台！"},
               {"type": "member", "nickname": "进场侠", "member_count": 233},
               {"type": "gift", "nickname": "土豪", "gift_name": "小心心", "num": 9},
               {"type": "like", "nickname": "赞神", "num": 50},
               {"type": "follow", "nickname": "新粉"}]:
        r = post("/api/inject", ev)
        assert r.get("ok"), r
    print("inject OK: 5 类事件全部受理")

    time.sleep(4.0)   # 礼物3s连击窗 + 点赞2s聚合窗，等 debounce 落墙
    h = get("/api/history")
    types = {e["data"]["type"] for e in h["events"] if e["kind"] == "event"}
    replies = [e for e in h["events"] if e["kind"] == "reply"]
    assert {"comment", "member", "gift", "like", "follow"} <= types, types
    assert replies, "没有产生 AI 回复"
    kinds = {r["data"]["type"] for r in replies}
    print("feed  OK: 事件类型", sorted(types), "| 回复", len(replies), "条", sorted(kinds))

    # AI 热更新
    r = post("/api/ai", {"persona": "冒烟测试人设", "min_interval": {"comment": 0.1}})
    assert r.get("ok")
    s = get("/api/state")
    assert s["ai"]["persona"] == "冒烟测试人设", s["ai"]["persona"]
    print("ai    OK: persona 热更新生效")

    # 模拟互动消息开关（先 stop 清残留，再 start）
    post("/api/sim/stop")
    assert post("/api/sim/start", {"interval": 0.3}).get("ok")
    time.sleep(1.2)
    assert post("/api/sim/stop").get("ok")
    s2 = get("/api/state")
    assert s2["events"]["received"] > 5, "模拟器没进事件"
    print("sim   OK: 模拟事件流入", s2["events"])

    # 预览帧
    with urllib.request.urlopen(BASE + "/preview.jpg", timeout=10) as r:
        jpg = r.read()
    assert jpg[:2] == b"\xff\xd8" and len(jpg) > 5000, "预览 JPEG 异常"
    print("prev  OK: JPEG", len(jpg), "bytes")

    print("WEB SMOKE TEST OK" if ok else "FAILED")


if __name__ == "__main__":
    main()
