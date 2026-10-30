"""独立「余额面板」渲染器：一路独立素材通道，伴侣上可任意拖放。

六格数据（全部来自白饭/余额账本，今日项 0 点清零）：
  余额 X 元     今日收入 +X 元    今日花费 -X 元
  今日白饭 X 碗  收到礼物 X 份     今日 Token X
1 块钱 = 1 碗白饭（碗 = 今日花费取整，可在 config render.balance.rice_from 改为 income）。

配置 config.render.balance：
  enabled  是否推送这一路        title    面板标题
  accent   主色 [r,g,b]         背景沿用弹幕墙 bg 风格
  rice_from  "spend"|"income"   白饭碗按花费还是收入算
"""
from __future__ import annotations

import time

from PIL import Image, ImageDraw

from ..ipc import Packet
from .character import load_font


def _money(v: float) -> str:
    v = float(v)
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s or "0"


class BalanceRenderer:
    def __init__(self, cfg: dict, width: int, height: int,
                 channel_id: int = 3) -> None:
        cfg = cfg or {}
        self.channel_id = channel_id
        self.width, self.height = width, height
        self.enabled = bool(cfg.get("enabled", True))
        self.title = str(cfg.get("title", "直播小账本"))
        self.accent = tuple(cfg.get("accent", (255, 214, 90))[:3]) + (255,)
        self.bg = tuple(cfg.get("bg", (12, 16, 30, 170)))
        self.rice_from = str(cfg.get("rice_from", "spend"))
        self.radius = cfg.get("radius")          # None=按高度自适应
        self.border_width = max(0, int(cfg.get("border_width", 2)))
        font_path = cfg.get("font_path", "C:/Windows/Fonts/msyh.ttc")
        self._fp = font_path
        # 文字大小缩放（0.5~2.0）：嫌挤就调小
        scale = max(0.5, min(2.0, float(cfg.get("font_scale", 1.0) or 1.0)))
        self.font_scale = scale
        h = height
        self._f_title = load_font(font_path, max(9, int(h * 0.13 * scale)))
        self._val_size = max(11, int(h * 0.20 * scale))
        self._f_val = load_font(font_path, self._val_size)
        self._f_lab = load_font(font_path, max(8, int(h * 0.11 * scale)))
        self._snap: dict = {}
        self._src = None               # callable -> 账本 snapshot（优先于 _snap）

    def set_stats(self, snap: dict) -> None:
        self._snap = dict(snap or {})

    def set_source(self, fn) -> None:
        """接一个 callable 返回账本快照，每帧自动取最新，无需外部同步。"""
        self._src = fn

    # -- 帧生成 ---------------------------------------------------------------
    def next_frame(self) -> Packet:
        w, h = self.width, self.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        rad = self.radius if self.radius is not None else int(h * 0.14)
        if self.border_width > 0:
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=rad, fill=self.bg,
                                outline=self.accent[:3] + (90,),
                                width=self.border_width)
        else:
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=rad, fill=self.bg)
        snap = self._src() if self._src is not None else self._snap
        cur = snap.get("currency") or "元"

        # 标题行：标题 + 日期
        d.text((int(w * 0.025), int(h * 0.04)), self.title,
               font=self._f_title, fill=self.accent)
        day = str(snap.get("day") or time.strftime("%Y-%m-%d"))[5:]
        d.text((w - int(w * 0.025) - d.textlength(day, font=self._f_lab),
                int(h * 0.07)), day, font=self._f_lab, fill=(255, 255, 255, 120))

        bal = float(snap.get("balance", 0) or 0)
        inc = float(snap.get("today_income", 0) or 0)
        spd = float(snap.get("today_spend", 0) or 0)
        bowls = int(spd if self.rice_from != "income" else inc)
        gifts = int(snap.get("today_gifts", 0) or 0)
        toks = int(snap.get("today_tokens", 0) or 0)
        cells = [
            ("余额", f"{_money(bal)} {cur}", self.accent[:3]),
            ("今日收入", f"+{_money(inc)}", (126, 224, 160)),
            ("今日花费", f"-{_money(spd)}", (255, 155, 138)),
            ("今日白饭", f"{bowls} 碗", (240, 230, 200)),
            ("收到礼物", f"{gifts} 份", (240, 160, 90)),
            ("Token", f"{toks:,}", (120, 190, 255)),
        ]

        top = int(h * 0.30)
        cell_w, cell_h = w // 3, (h - top) // 2
        for i, (lab, val, color) in enumerate(cells):
            r_, c_ = divmod(i, 3)
            cx = c_ * cell_w + cell_w // 2
            cy = top + r_ * cell_h
            # 数值：居中，超宽自动缩字号
            size = self._val_size
            fval = self._f_val
            while size > 12 and d.textlength(val, font=fval) > cell_w - 12:
                size -= 1
                fval = load_font(self._fp, size)
            vw = d.textlength(val, font=fval)
            d.text((cx - vw / 2, cy + int(cell_h * 0.10)), val,
                   font=fval, fill=tuple(color) + (240,))
            lw = d.textlength(lab, font=self._f_lab)
            d.text((cx - lw / 2, cy + int(cell_h * 0.62)), lab,
                   font=self._f_lab, fill=(255, 255, 255, 130))
        return Packet.make_video(self.channel_id, img.tobytes(), w, h, fps=30)
