"""弹幕墙装修层离线测试：标题/公告轮播/模板/类型开关/背景图。

    python tools/test_render_deco.py
渲染帧会保存到系统临时目录（PNG），可肉眼检查装修效果。
"""
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

from liveplugin.render import DanmakuRenderer


def ev(**kw):
    base = dict(type="comment", nickname="观众", content="", gift_name="", gift_num=1,
                gift_value=0, like_num=0, follow_action=0, fansclub_reason=0,
                fansclub_level=0, member_count=0, msg_id="")
    base.update(kw)
    base["total_value"] = base["gift_value"] * base["gift_num"]   # LiveEvent 属性
    return SimpleNamespace(**base)


def main():
    tmp = Path(tempfile.gettempdir())
    # 造一张透明背景图（带贴纸图案）
    bgp = tmp / "deco_bg_test.png"
    im = Image.new("RGBA", (310, 450), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, 309, 449], radius=40, fill=(40, 10, 60, 190))
    d.ellipse([230, 20, 290, 80], fill=(255, 80, 120, 220))
    im.save(bgp)

    cfg = {
        "max_lines": 8, "enter_seconds": 0.0,   # 关掉入场位移便于比对
        "bg": [10, 20, 40, 160],
        "title": {"enabled": True, "text": "小抖の直播间", "color": [255, 220, 120]},
        "banners": [{"text": "点关注不迷路～"}, {"text": "每晚8点开播"}],
        "banner_seconds": 2.0,
        "radius": 32, "border": {"enabled": True, "width": 3},
        "font_scale": 1.15,
        "bg_image": str(bgp),                    # 绝对路径
        "show_types": {"like": False, "room_stats": False},
        "templates": {"gift": "霸气送出 {gift_name}",
                      "member": "{nickname} 来了"},
    }
    r = DanmakuRenderer(cfg, 620, 900, 1)
    assert r._bg_img is not None, "背景图未加载"

    r.push_event(ev(type="comment", nickname="小美", content="装修效果不错！"))
    r.push_event(ev(type="gift", nickname="土豪哥", gift_name="小心心",
                    gift_num=5, gift_value=1))
    r.push_event(ev(type="member", nickname="新观众", member_count=99))
    r.push_event(ev(type="like", nickname="点赞狂", like_num=50))   # 被开关拦截
    r.push_event(ev(type="follow", nickname="新粉", follow_action=1))
    n_items = len(r._items)
    assert n_items == 4, f"show_types 未拦截 like：{n_items}"

    # 模板生效检查
    texts = [it.text for it in r._items]
    assert "霸气送出 小心心" in texts, texts
    assert "新观众 来了" in texts, texts

    frame = r.next_frame()
    img = Image.frombytes("RGBA", (620, 900), frame.data)
    assert frame.video.cx == 620 and frame.video.cy == 900
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    assert sum(hist[1:]) > 620 * 900 * 0.5, "面板像素覆盖异常（背景图未渲染？）"
    # 背景图贴纸存在（右上区域有 ~255,80,120 像素；LANCZOS 缩放有 ±1 色差）
    px = img.crop((460, 30, 580, 150)).get_flattened_data()
    assert any(abs(p[0] - 255) <= 3 and abs(p[1] - 80) <= 3 and abs(p[2] - 120) <= 3
               for p in px), "背景图贴纸未出现"

    out1 = tmp / "deco_frame_1.png"
    out2 = tmp / "deco_frame_2.png"
    img.save(out1)
    time.sleep(2.2)                               # 跨过轮播间隔
    img2 = Image.frombytes("RGBA", (620, 900), r.next_frame().data)
    img2.save(out2)

    # 纯色底模式（无背景图）也要能画
    r2 = DanmakuRenderer(dict(cfg, bg_image=""), 620, 900, 1)
    r2.push_event(ev(type="comment", content="纯色底"))
    f2 = r2.next_frame()
    assert f2.data and len(f2.data) == 620 * 900 * 4

    print(f"装修帧已保存：{out1} / {out2}（公告应轮换）")
    print("RENDER DECO TEST OK")


if __name__ == "__main__":
    main()
