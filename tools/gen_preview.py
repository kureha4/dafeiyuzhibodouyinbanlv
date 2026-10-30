import os
os.makedirs("D:/ProjectFile/LiveStreaming/preview", exist_ok=True)
from PIL import Image
from liveplugin.config import Config
from liveplugin.render import CharacterRenderer, DanmakuRenderer
from liveplugin.events import LiveEvent

cfg = Config.load()
ch = cfg["channels"]

# 人物帧：idle / speaking / thanks
cr = CharacterRenderer(cfg["render"], ch["character"]["width"], ch["character"]["height"])
for st in ("idle", "speaking", "thanks"):
    cr.set_state(st, 5)
    pkt = cr.next_frame()
    img = Image.frombytes("RGBA", (pkt.video.cx, pkt.video.cy), pkt.data)
    bg = Image.new("RGBA", img.size, (40, 44, 64, 255))
    bg.alpha_composite(img)
    bg.convert("RGB").save(f"D:/ProjectFile/LiveStreaming/preview/char_{st}.png")

# 弹幕墙帧
dm = DanmakuRenderer(
    dict(cfg["render"]["danmaku"], font_path=cfg["render"]["font_path"]),
    ch["danmaku"]["width"], ch["danmaku"]["height"])
dm.push_event(LiveEvent(type="comment", nickname="阿泽", content="主播唱一首起风了"))
dm.push_event(LiveEvent(type="gift", nickname="momo", gift_name="玫瑰", gift_num=10, gift_value=1))
dm.push_event(LiveEvent(type="like", nickname="追风少年", like_num=15))
dm.push_event(LiveEvent(type="follow", nickname="柚子茶", follow_action=1))
dm.push_event(LiveEvent(type="fansclub", nickname="深夜食客", fansclub_reason=2, fansclub_level=2))
dm.push_reply("小抖", "起风了这首歌我马上唱~ 阿泽你想听吗？")
import time as _t; _t.sleep(0.5)   # 等入场动效结束再截图
pkt = dm.next_frame()
img = Image.frombytes("RGBA", (pkt.video.cx, pkt.video.cy), pkt.data)
bg = Image.new("RGBA", img.size, (40, 44, 64, 255))
bg.alpha_composite(img)
bg.convert("RGB").save("D:/ProjectFile/LiveStreaming/preview/danmaku.png")
print("saved:", os.listdir("D:/ProjectFile/LiveStreaming/preview"))
