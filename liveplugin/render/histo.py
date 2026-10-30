"""「音乐频谱」面板：随本机实际出声跳动的直方图（数据来自 audiospectrum 环回采集）。

配置 config.render.histo：
  enabled   是否推送这一路
  bars      柱数（8~64）
  bg        面板底色 RGBA        accent/accent2  渐变两端色
  radius / border_width 同其它面板
  peak_cap  柱顶余辉小横杆（默认开）
数据入口：set_source(fn) → fn() 返回 N 个 0.0~1.0 电平（无信号时全 0）。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from ..ipc import Packet


class HistoRenderer:
    def __init__(self, cfg: dict, width: int, height: int,
                 channel_id: int = 6) -> None:
        cfg = cfg or {}
        self.channel_id = channel_id
        self.width, self.height = width, height
        self.enabled = bool(cfg.get("enabled", True))
        self.bars = max(8, min(64, int(cfg.get("bars", 28) or 28)))
        self.bg = tuple(cfg.get("bg", (12, 16, 30, 150)))
        self.accent = tuple(cfg.get("accent", (126, 200, 255))[:3])
        self.accent2 = tuple(cfg.get("accent2", (255, 110, 190))[:3])
        self.radius = cfg.get("radius")
        self.border_width = max(0, int(cfg.get("border_width", 2)))
        self.peak_cap = bool(cfg.get("peak_cap", True))
        self._src = None
        self._peaks = [0.0] * self.bars

    def set_source(self, fn) -> None:
        self._src = fn

    def _color(self, lv: float, i: int) -> tuple:
        # 左右渐变 + 越高越亮：从 accent 平滑过渡到 accent2
        t = (i / max(1, self.bars - 1)) * 0.65 + lv * 0.35
        c = tuple(int(a + (b - a) * t) for a, b in zip(self.accent, self.accent2))
        return c

    def next_frame(self) -> Packet:
        w, h = self.width, self.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        rad = self.radius if self.radius is not None else int(h * 0.10)
        if self.border_width > 0:
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=rad, fill=self.bg,
                                outline=self.accent[:3] + (90,),
                                width=self.border_width)
        else:
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=rad, fill=self.bg)

        lv = list(self._src()) if self._src is not None else [0.0] * self.bars
        if len(lv) != self.bars:            # 柱数改了：两边同步
            self.bars = len(lv)
            self._peaks = [0.0] * self.bars
        pad_x = int(w * 0.045)
        pad_y = int(h * 0.12)
        base = h - pad_y                    # 柱子基线
        area = w - 2 * pad_x
        gap = max(1, int(area * 0.02))
        bw = max(2, (area - gap * (self.bars - 1)) // self.bars)
        off = (area - (bw * self.bars + gap * (self.bars - 1))) // 2
        maxh = base - pad_y

        # 基线
        d.line([(pad_x + off, base), (pad_x + off + area, base)],
               fill=(255, 255, 255, 45), width=1)

        for i, v in enumerate(lv):
            v = max(0.0, min(1.0, float(v)))
            self._peaks[i] = max(v, self._peaks[i] * 0.94)
            bh = max(2, int(maxh * v))
            x0 = pad_x + off + i * (bw + gap)
            c = self._color(v, i)
            d.rounded_rectangle([x0, base - bh, x0 + bw - 1, base - 1],
                                radius=min(bw // 2, 6), fill=c + (235,))
            if self.peak_cap and self._peaks[i] > 0.05:
                py = base - max(2, int(maxh * self._peaks[i])) - 4
                d.rectangle([x0, py, x0 + bw - 1, py + 2], fill=c + (150,))
        return Packet.make_video(self.channel_id, img.tobytes(), w, h, fps=30)
