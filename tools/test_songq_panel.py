"""点歌队列面板离线渲染 + 像素验证（无屏幕环境用像素扫描代替肉眼）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liveplugin.render import SongQueueRenderer

cfg = {"enabled": True, "title": "点歌队列",
       "accent": [126, 200, 255], "bg": [12, 16, 30, 170]}
r = SongQueueRenderer(cfg, 520, 300, channel_id=4)
r.set_source(lambda: {
    "playing": True,
    "current": {"name": "晴天", "artist": "周杰伦", "nickname": "小明",
                "elapsed": 12.0, "limit": 30},
    "queue": [{"name": "一路生花", "artist": "温奕心", "nickname": "小红"},
              {"name": "海阔天空", "artist": "Beyond", "nickname": "阿强"}],
})
pkt = r.next_frame()
assert pkt.channel_id == 4 and (pkt.video.cx, pkt.video.cy) == (520, 300)
from PIL import Image
img = Image.frombytes("RGBA", (520, 300), pkt.data)
img.save("preview/_songq_test.png")

px = img.load()
W, H = 520, 300


def count_color(pred, step=2):
    n = 0
    for y in range(0, H, step):
        for x in range(0, W, step):
            if pred(px[x, y]):
                n += 1
    return n


# 1) 面板底存在（左上圆角内侧）
bg_in = px[30, 150]
assert abs(bg_in[0] - 12) < 12 and abs(bg_in[1] - 16) < 12, bg_in
# 2) 主色（accent 蓝）出现：标题+徽标+进度条填充
accent_n = count_color(lambda c: c[2] > 190 and c[0] < 165 and c[1] > 165
                       and c[3] > 150)
assert accent_n > 25, accent_n
# 3) 进度条在 y≈0.255*300=76 附近，12/30 → 填充到 x≈pad+(W-2pad)*0.4≈204
bar_fill = px[150, 78]      # 填充区内
bar_rest = px[420, 78]      # 未填充区（应接近半透明白底轨）
assert bar_fill[2] > 190 and bar_fill[0] < 170, ("bar fill", bar_fill)
assert not (bar_rest[2] > 190 and bar_rest[0] < 170), ("bar rest", bar_rest)
# 4) 白色文字像素存在（歌名/排队行）
white_n = count_color(lambda c: c[0] > 200 and c[1] > 200 and c[2] > 200
                      and c[3] > 150)
assert white_n > 60, white_n
print("SONGQ PANEL TEST OK  accent=", accent_n, "white=", white_n)
