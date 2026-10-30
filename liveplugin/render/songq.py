"""「点歌队列」面板：正在播放（歌名+进度条+点播人）+ 排队列表。

数据来源 jukebox.panel()，每帧热读，无需手动同步。

配置 config.render.songq：
  enabled  是否推送这一路      title   面板标题
  accent   主色 [r,g,b]（进度条/标题）      bg  面板底色
  radius / border_width 同余额面板
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from ..ipc import Packet
from .character import load_font


class SongQueueRenderer:
    def __init__(self, cfg: dict, width: int, height: int,
                 channel_id: int = 4) -> None:
        cfg = cfg or {}
        self.channel_id = channel_id
        self.width, self.height = width, height
        self.enabled = bool(cfg.get("enabled", True))
        self.title = str(cfg.get("title", "点歌队列"))
        self.accent = tuple(cfg.get("accent", (126, 200, 255))[:3]) + (255,)
        self.bg = tuple(cfg.get("bg", (12, 16, 30, 170)))
        self.radius = cfg.get("radius")
        self.border_width = max(0, int(cfg.get("border_width", 2)))
        self._fp = cfg.get("font_path", "C:/Windows/Fonts/msyh.ttc")
        h = height
        scale = max(0.5, min(2.0, float(cfg.get("font_scale", 1.0) or 1.0)))
        self._scale = scale
        self._f_title = load_font(self._fp, max(11, int(h * 0.085 * scale)))
        self._cur_size = max(11, int(h * 0.072 * scale))
        self._f_row = load_font(self._fp, max(9, int(h * 0.058 * scale)))
        self._data: dict = {"playing": False, "current": None, "queue": []}
        self._src = None

    def set_source(self, fn) -> None:
        self._src = fn

    def set_data(self, d: dict) -> None:
        self._data = dict(d or {})

    # -- 文字适配：先缩字号，仍超宽则截断加省略号 --------------------------------
    def _fit(self, d, text: str, size: int, maxw: int):
        f = load_font(self._fp, size)
        while size > 11 and d.textlength(text, font=f) > maxw:
            size -= 1
            f = load_font(self._fp, size)
        if d.textlength(text, font=f) > maxw:
            while text and d.textlength(text + "…", font=f) > maxw:
                text = text[:-1]
            text += "…"
        return text, f

    # -- 帧生成 -----------------------------------------------------------------
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
        data = self._src() if self._src is not None else self._data

        pad = int(w * 0.03)
        # 标题 + 首数徽标
        d.text((pad, int(h * 0.030)), self.title, font=self._f_title,
               fill=self.accent)
        queue = list(data.get("queue") or [])
        cnt = ("正在播放 " if data.get("playing") else "等待 ") + \
              f"{1 + len(queue) if data.get('playing') else len(queue)} 首"
        d.text((w - pad - d.textlength(cnt, font=self._f_row), int(h * 0.045)),
               cnt, font=self._f_row, fill=(255, 255, 255, 120))

        # 正在播放
        cur = data.get("current")
        if cur:
            lab = f"♪ {cur.get('name', '')}"
            if cur.get("artist"):
                lab += f" - {cur['artist']}"
            t, f = self._fit(d, lab, self._cur_size, w - 2 * pad)
            d.text((pad, int(h * 0.155)), t, font=f, fill=(255, 255, 255, 240))
            # 进度条
            limit = max(1.0, float(cur.get("limit", 30) or 30))
            el = min(limit, max(0.0, float(cur.get("elapsed", 0) or 0)))
            x0, x1 = pad, w - pad
            by, bh = int(h * 0.255), max(4, int(h * 0.020))
            d.rounded_rectangle([x0, by, x1, by + bh], radius=bh // 2,
                                fill=(255, 255, 255, 40))
            px = x0 + int((x1 - x0) * el / limit)
            d.rounded_rectangle([x0, by, max(x0 + bh, px), by + bh],
                                radius=bh // 2, fill=self.accent[:3] + (225,))
            meta = (f"{int(el)}/{int(limit)}s"
                    f"　点播：{cur.get('nickname', '观众')}")
            mt, mf = self._fit(d, meta, max(9, int(h * 0.055 * self._scale)),
                               w - 2 * pad)
            d.text((pad, int(h * 0.290)), mt, font=mf, fill=(255, 255, 255, 150))
        else:
            t = "暂无点歌，发「点歌 歌名」即可上队"
            mt, mf = self._fit(d, t, max(10, int(h * 0.062 * self._scale)),
                               w - 2 * pad)
            d.text((pad, int(h * 0.18)), mt, font=mf, fill=(255, 255, 255, 130))

        # 排队列表（当前歌算第 1 首，所以排队从 2 开始编号）
        qy = int(h * 0.40)
        d.text((pad, qy), "排队中", font=self._f_row,
               fill=self.accent[:3] + (180,))
        row_h = max(16, int(h * 0.105))
        avail = (h - qy - row_h - int(h * 0.03)) // row_h
        for i, s in enumerate(queue[:max(1, avail)]):
            y = qy + row_h * (i + 1)
            line = f"{i + 2}. {s.get('name', '')}"
            if s.get("artist"):
                line += f" - {s['artist']}"
            nick = f"←{s.get('nickname', '')}"
            nw = d.textlength(nick, font=self._f_row)
            t, f = self._fit(d, line, max(10, int(h * 0.062 * self._scale)),
                             w - 2 * pad - nw - 8)
            d.text((pad, y), t, font=f, fill=(255, 255, 255, 200))
            d.text((w - pad - nw, y), nick, font=self._f_row,
                   fill=(255, 255, 255, 110))
        return Packet.make_video(self.channel_id, img.tobytes(), w, h, fps=30)
