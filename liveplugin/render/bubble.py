"""「AI 气泡」：把大模型回复渲染成对话气泡（圆角框 + 小尾巴），自动过期。

配置 config.render.bubble：
  enabled   是否推送这一路
  bg        气泡底色 [r,g,b,a]
  text      文字颜色 [r,g,b]
  accent    描边色 [r,g,b]
  ttl       一条气泡显示秒数（默认 8）
  tail      尾巴方向 up/down/none（默认 up，气泡在角色下方时指向上方）
  font_scale 文字大小 ×（0.5~2）
数据入口：show(text) —— AI 回复到达时调用（线程安全）。
"""
from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

from ..ipc import Packet
from .character import load_font


class BubbleRenderer:
    def __init__(self, cfg: dict, width: int, height: int,
                 channel_id: int = 5) -> None:
        cfg = cfg or {}
        self.cfg = dict(cfg)
        self.channel_id = channel_id
        self.width, self.height = width, height
        self.enabled = bool(cfg.get("enabled", True))
        self.bg = tuple(cfg.get("bg", (20, 26, 48, 225)))
        self.text_color = tuple(cfg.get("text", (255, 255, 255))[:3])
        self.accent = tuple(cfg.get("accent", (126, 200, 255))[:3])
        self.tail = str(cfg.get("tail", "up"))
        self.ttl = max(2.0, float(cfg.get("ttl", 8.0) or 8.0))
        self._fp = cfg.get("font_path", "C:/Windows/Fonts/msyh.ttc")
        scale = max(0.5, min(2.0, float(cfg.get("font_scale", 1.0) or 1.0)))
        self._size = max(12, int(height * 0.115 * scale))
        self._lock = threading.Lock()
        self._text = ""
        self._t0 = 0.0

    # -- 数据入口（AI 线程调用） -------------------------------------------------
    def show(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        with self._lock:
            self._text = text
            self._t0 = time.time()

    def clear(self) -> None:
        with self._lock:
            self._text = ""

    # -- 文字换行：按宽度切成最多 max_lines 行，装不下用省略号 --------------------
    def _wrap(self, d, text: str, font, maxw: int, max_lines: int):
        lines: list[str] = []
        i, n = 0, len(text)
        while len(lines) < max_lines and i < n:
            cur = ""
            while i < n and d.textlength(cur + text[i], font=font) <= maxw:
                cur += text[i]
                i += 1
            if i < n and len(lines) == max_lines - 1:
                # 最后一行还装不下：省略号收尾
                while cur and d.textlength(cur + "…", font=font) > maxw:
                    cur = cur[:-1]
                cur += "…"
                lines.append(cur)
                break
            if not cur:            # 单字符都超宽，硬吃一个防止死循环
                cur = text[i]
                i += 1
            lines.append(cur)
        return lines

    # -- 帧生成 -------------------------------------------------------------------
    def next_frame(self) -> Packet:
        w, h = self.width, self.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        with self._lock:
            text, t0 = self._text, self._t0
        if not text:
            return Packet.make_video(self.channel_id, img.tobytes(), w, h,
                                     fps=30)
        age = time.time() - t0
        if age > self.ttl:
            return Packet.make_video(self.channel_id, img.tobytes(), w, h,
                                     fps=30)
        # 进出场淡入淡出（气泡自己的透明度，跟音乐音量无关）
        a = 1.0
        if age < 0.3:
            a = age / 0.3
        elif age > self.ttl - 1.0:
            a = max(0.0, (self.ttl - age) / 1.0)
        alpha = int(255 * a)

        font = load_font(self._fp, self._size)
        pad = max(10, int(h * 0.07))
        tail_h = 14 if self.tail in ("up", "down") else 0
        max_lines = max(2, (h - 2 * pad - tail_h) // (self._size + 6))
        lines = self._wrap(d, text, font, w - 2 * pad, max_lines)
        line_h = self._size + 6
        box_h = min(h - tail_h, 2 * pad + line_h * len(lines))
        if self.tail == "up":
            top, bot = tail_h, tail_h + box_h
        elif self.tail == "down":
            top, bot = 0, box_h
        else:
            top, bot = 0, box_h
        rad = max(8, int(box_h * 0.16))
        bg = tuple(self.bg[:3]) + (int(self.bg[3] if len(self.bg) > 3 else 225)
                                   * alpha // 255,)
        d.rounded_rectangle([0, top, w - 1, bot - 1], radius=rad, fill=bg,
                            outline=self.accent[:3] + (int(120 * a),), width=2)
        if tail_h:
            tx = int(w * 0.18)
            if self.tail == "up":
                d.polygon([(tx, top + 1), (tx + tail_h, top + 1),
                           (tx + tail_h // 2, top - tail_h + 2)], fill=bg)
            else:
                d.polygon([(tx, bot - 2), (tx + tail_h, bot - 2),
                           (tx + tail_h // 2, bot + tail_h - 2)], fill=bg)
        ty = top + pad
        fill = self.text_color + (alpha,)
        for ln in lines:
            d.text((pad, ty), ln, font=font, fill=fill)
            ty += line_h
        return Packet.make_video(self.channel_id, img.tobytes(), w, h, fps=30)
