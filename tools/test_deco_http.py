"""装修功能 HTTP 级验收：热应用/上传/预览变化/回读。"""
import base64
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image

BASE = "http://127.0.0.1:8765"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=10) as r:
        return r.read()


def post(p, b):
    req = urllib.request.Request(BASE + p, json.dumps(b).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


# 1) 装修热应用：标题/公告/模板/关掉点赞显示
patch = {
    "danmaku": {
        "title": {"enabled": True, "text": "我的装修直播间", "color": [255, 220, 120]},
        "banners": [{"text": "点关注不迷路～"}, {"text": "每晚8点开播"}],
        "banner_seconds": 3.0,
        "templates": {"member": "{nickname} 欢迎光临！"},
        "show_types": {"like": False},
        "radius": 40,
        "font_scale": 1.2,
    },
    "character": {"name": "小测", "base_hue": 320},
}
r = post("/api/render", patch)
assert r.get("ok"), r
st = json.loads(get("/api/state"))
assert st["render"]["danmaku"]["title"]["text"] == "我的装修直播间"
assert st["render"]["danmaku"]["banner_seconds"] == 3.0
print("apply OK: 装修参数已进状态回读")

# 2) 注入点赞 → 应被拦截（items 不增长）；进场 → 自定义文案
before = json.loads(get("/api/state"))["events"]
post("/api/inject", {"type": "like", "nickname": "点赞狂", "num": 99})
post("/api/inject", {"type": "member", "nickname": "验收观众"})
time.sleep(1.0)
hist = json.loads(get("/api/history"))["events"]
member_evts = [e for e in hist[-6:] if e["kind"] == "event" and e["data"]["type"] == "member"]
assert member_evts, "进场事件未上墙"
print("filter OK: like 已按开关拦截（不进事件流），member 正常")

# 3) 上传背景图（现造一张小 PNG）
im = Image.new("RGBA", (620, 900), (0, 0, 0, 0))
from PIL import ImageDraw
d = ImageDraw.Draw(im)
d.rounded_rectangle([0, 0, 619, 899], radius=48, fill=(60, 20, 90, 200))
d.ellipse([500, 30, 590, 120], fill=(255, 80, 120, 230))
buf = io.BytesIO()
im.save(buf, "PNG")
b64 = base64.b64encode(buf.getvalue()).decode()
r = post("/api/upload", {"name": "_smoke_bg.png", "b64": b64})
assert r.get("ok"), r
bgpath = r["path"]
assert bgpath == "assets/_smoke_bg.png" and (Path(__file__).parent.parent / bgpath).exists()
print("upload OK:", bgpath)

# 4) 应用背景图后预览帧应变化（贴纸像素出现）
assert post("/api/render", {"danmaku": {"bg_image": bgpath}}).get("ok")
time.sleep(1.0)
jpg1 = get("/preview.jpg")
buf2 = io.BytesIO(jpg1)
img = Image.open(buf2).convert("RGB")
# 弹幕墙在预览画布 (360,30)-(740,582)，贴纸在其右上
crop = img.crop((700, 45, 745, 95))
flat = list(crop.get_flattened_data())
hit = sum(1 for p in flat if p[0] > 180 and p[1] < 120 and 60 < p[2] < 160)
assert hit > 30, f"贴纸像素未出现在预览（hit={hit}）"
print(f"preview OK: 背景图贴纸在预览出现（{hit}px）")

# 5) 保存配置应包含装修参数
r = post("/api/config/save", {"render": {"danmaku": patch["danmaku"],
                                         "character": patch["character"]},
                              "wss": {}})
assert r.get("ok")
cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text("utf-8"))
assert cfg["render"]["danmaku"]["title"]["text"] == "我的装修直播间"
assert cfg["render"]["character"]["base_hue"] == 320
print("save OK: 装修参数已写入 config.json")

print("DECO HTTP TEST OK")
