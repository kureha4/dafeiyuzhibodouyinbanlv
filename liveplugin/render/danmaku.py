"""装修版弹幕墙渲染器（非官方样式，完全自绘）。

数据来源就是插件自己订阅到的互动事件 + AI 回复，渲染成 RGBA 帧推到素材通道：
- 半透明圆角面板底板（装修样式，可调颜色/透明度/圆角/描边/背景图）
- 每条弹幕带类型色徽章（评论/礼物/点赞/关注/粉丝团）与入场动效
- 礼物条目高亮并显示"xN"与价值
- AI 回复以醒目气泡置顶区分（主播说的话），形成"对话感"
- 固定行高滚动墙：面板恒为 max_lines 个槽位，弹幕从底部进入、不断往上轮，
  满了顶出最旧一条；行高与当前条数无关（不再"条少撑大、条多挤小"）
- 人气（room_stats）不占弹幕槽位，实时刷在标题栏右侧「在线 N」角标

装修自定义（config.render.danmaku，全部可选、缺省即旧样式）：
- title    {enabled, text, color:[r,g,b]}      顶部标题（换成你的直播间名）
- banners  [{text, color:[r,g,b]}]             底部公告条，多条自动轮播
- banner_seconds  轮播间隔秒
- bg_image "assets/xx.png"                     透明背景图替代纯色底
- radius   圆角像素        border {enabled,width,color}
- font_scale 字体整体缩放   show_types {comment:true,...} 按类型开关
- templates {gift:"送出 {gift_name}", like/follow/member} 文案自定义
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw

from ..ipc import Packet
from .character import load_font

_ROOT = Path(__file__).resolve().parents[2]     # 项目根（解析 assets/ 相对路径）


class _Item:
    __slots__ = ("kind", "nickname", "text", "sub", "color", "t0", "life")

    def __init__(self, kind, nickname, text, sub, color, t0, life):
        self.kind = kind
        self.nickname = nickname
        self.text = text
        self.sub = sub
        self.color = color
        self.t0 = t0
        self.life = life


class DanmakuRenderer:
    def __init__(self, cfg: dict, width: int, height: int, channel_id: int = 1) -> None:
        self.cfg = cfg or {}
        self.width = width
        self.height = height
        self.channel_id = channel_id
        self.max_lines = max(3, min(30, int(cfg.get("max_lines", 12))))
        self.enter = float(cfg.get("enter_seconds", 0.35))
        self.bg = tuple(cfg.get("bg", (12, 16, 30, 150)))
        colors = cfg.get("bubble_colors", {}) or {}
        self._colors = {
            "comment": tuple(colors.get("comment", (46, 156, 219))),
            "gift": tuple(colors.get("gift", (240, 134, 58))),
            "like": tuple(colors.get("like", (214, 90, 140))),
            "follow": tuple(colors.get("follow", (87, 185, 138))),
            "fansclub": tuple(colors.get("fansclub", (150, 120, 220))),
            "member": tuple(colors.get("member", (150, 120, 220))),
            "reply": (255, 214, 90),       # AI 回复金色
        }
        self._items: deque[_Item] = deque()
        self._online = 0                       # room_stats 角标（不占弹幕行）
        self._balance = None                   # 余额角标（标题栏）
        self._balance_currency = ""
        self.show_balance = bool(cfg.get("show_balance", True))
        self._font_path = cfg.get("font_path", "C:/Windows/Fonts/msyh.ttc")

        # ---- 装修自定义层（缺省 = 原样式） ----------------------------------
        t = cfg.get("title") or {}
        self.title_enabled = bool(t.get("enabled", True))
        self.title_text = str(t.get("text", "互动弹幕墙")) or " "
        self.title_color = tuple(t.get("color", (255, 255, 255))[:3]) + (225,)

        self.banners = [b for b in (cfg.get("banners") or [])
                        if isinstance(b, dict) and str(b.get("text", "")).strip()]
        self.banner_seconds = max(1.5, float(cfg.get("banner_seconds", 6.0)))

        self.radius = cfg.get("radius", None)
        if self.radius is not None:
            self.radius = max(0, int(self.radius))
        bd = cfg.get("border") or {}
        self.border_enabled = bool(bd.get("enabled", True))
        self.border_width = max(0, int(bd.get("width", 2)))
        self.border_color = tuple(bd.get("color", (255, 255, 255))[:3]) + (40,)

        self.show_types = {k: bool(v) for k, v in (cfg.get("show_types") or {}).items()}
        self.templates = dict(cfg.get("templates") or {})

        scale = max(0.6, min(2.0, float(cfg.get("font_scale", 1.0))))
        self._f_title = load_font(self._font_path, max(16, int(height * 0.026 * scale)))
        self._font_main = load_font(self._font_path, max(16, int(height * 0.028 * scale)))
        self._font_small = load_font(self._font_path, max(13, int(height * 0.020 * scale)))
        self._font_nick = load_font(self._font_path, max(14, int(height * 0.023 * scale)))

        # 背景图（透明 PNG 优先）；路径相对项目根或绝对路径
        self._bg_img: Image.Image | None = None
        p = cfg.get("bg_image")
        if p:
            path = Path(str(p))
            if not path.is_absolute():
                path = _ROOT / path
            if path.exists():
                try:
                    im = Image.open(path).convert("RGBA")
                    self._bg_img = im.resize((width, height), Image.LANCZOS)
                except Exception:
                    self._bg_img = None

    # -- 外部投递 -------------------------------------------------------------
    def push_event(self, ev) -> None:      # ev: LiveEvent
        if not self.show_types.get(ev.type, True):
            return
        now = time.time()
        color = self._colors.get(ev.type, self._colors["comment"])
        if ev.type == "comment":
            self._add("comment", ev.nickname, ev.content, "", color, now, life=11.0)
        elif ev.type == "gift":
            ctx = {"nickname": ev.nickname, "gift_name": ev.gift_name,
                   "gift_num": ev.gift_num, "value": ev.total_value}
            self._add("gift", ev.nickname,
                      self._tpl("gift", ctx, f"送出 {ev.gift_name}"),
                      f"x{ev.gift_num} · {ev.total_value}抖币", color, now, life=13.0)
        elif ev.type == "like":
            ctx = {"nickname": ev.nickname, "like_num": ev.like_num}
            self._add("like", ev.nickname, self._tpl("like", ctx, "点赞"),
                      f"x{ev.like_num}", color, now, life=7.0)
        elif ev.type == "follow":
            default = "关注了主播" if ev.follow_action == 1 else "取消关注"
            key = "follow" if ev.follow_action == 1 else "unfollow"
            ctx = {"nickname": ev.nickname}
            self._add("follow", ev.nickname, self._tpl(key, ctx, default),
                      "", color, now, life=9.0)
        elif ev.type == "fansclub":
            reason = {1: "粉丝团升级", 2: "加入粉丝团", 16: "退出粉丝团"}.get(
                ev.fansclub_reason, "粉丝团变更")
            self._add("fansclub", ev.nickname, reason,
                      f"Lv.{ev.fansclub_level}" if ev.fansclub_level else "",
                      color, now, life=9.0)
        elif ev.type == "member":
            # WSS 逆向独有：进入直播间
            sub = f"在线 {ev.member_count}" if ev.member_count else ""
            ctx = {"nickname": ev.nickname, "member_count": ev.member_count}
            self._add("member", ev.nickname, self._tpl("member", ctx, "进入直播间"),
                      sub, self._colors["member"], now, life=7.0)
        elif ev.type == "room_stats":
            # 人气数字不占弹幕槽位，刷进标题角标
            self._online = ev.member_count or self._online

    def push_reply(self, nickname: str, text: str, is_self: bool = True) -> None:
        """AI/主播回复单独气泡，置顶显示形成对话感。"""
        now = time.time()
        if is_self:
            self._add("reply", "主播·AI回复", text, "",
                      self._colors["reply"], now, life=13.0)
        else:
            self._add("reply", nickname, text, "", self._colors["reply"],
                      now, life=13.0)

    def _tpl(self, key: str, ctx: dict, default: str) -> str:
        t = self.templates.get(key)
        if not t:
            return default
        try:
            return str(t).format(**ctx)
        except Exception:
            return default

    def _add(self, kind, nickname, text, sub, color, now, life) -> None:
        self._items.append(_Item(kind, nickname, _clip(text, 26), sub, color, now, life))
        while len(self._items) > self.max_lines:
            self._items.popleft()

    def set_balance(self, value, currency: str = "") -> None:
        """更新标题栏余额角标（None=不显示）。"""
        try:
            self._balance = float(value) if value is not None else None
        except (TypeError, ValueError):
            self._balance = None
        self._balance_currency = str(currency or "")

    # -- 帧生成 ---------------------------------------------------------------
    def next_frame(self) -> Packet:
        now = time.time()
        w, h = self.width, self.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        # 面板底板：背景图优先，否则纯色圆角+描边
        if self._bg_img is not None:
            img = self._bg_img.copy()
        else:
            rad = self.radius if self.radius is not None else int(h * 0.03)
            d0 = ImageDraw.Draw(img)
            if self.border_enabled and self.border_width > 0:
                d0.rounded_rectangle([0, 0, w - 1, h - 1], radius=rad,
                                     fill=self.bg, outline=self.border_color,
                                     width=self.border_width)
            else:
                d0.rounded_rectangle([0, 0, w - 1, h - 1], radius=rad, fill=self.bg)
        d = ImageDraw.Draw(img)
        # 顶部标题 + 右侧在线人数角标
        if self.title_enabled:
            d.text((int(w*0.04), int(h*0.022)), self.title_text,
                   font=self._f_title, fill=self.title_color)
            if self._online:
                lab = f"在线 {self._online:,}"
                lw = d.textlength(lab, font=self._font_small)
                d.text((w - int(w*0.04) - lw, int(h*0.028)), lab,
                       font=self._font_small, fill=(255, 255, 255, 150))
            if self._balance is not None and self.show_balance:
                b = self._balance
                btxt = (f"{b:,.0f}" if float(b).is_integer() else f"{b:,.2f}")
                cur = f" {self._balance_currency}" if self._balance_currency else ""
                blab = f"余额 {btxt}{cur}"
                bw = d.textlength(blab, font=self._font_small)
                right = w - int(w*0.04) - bw
                if self._online:
                    right -= d.textlength(f"在线 {self._online:,}",
                                          font=self._font_small) + int(w*0.02)
                d.text((max(int(w*0.04), right), int(h*0.028)), blab,
                       font=self._font_small, fill=(255, 214, 90, 220))

        # 固定滚动墙：消息保留到被更新的顶出（max_lines 上限），不按时间清除

        top = int(h * 0.075) if self.title_enabled else int(h * 0.02)
        bottom = int(h * 0.06) if self.banners else int(h * 0.02)
        row_area = h - top - bottom

        # 固定行高：行高只由 max_lines 决定，与当前条数无关。
        # 弹幕从底部进入、向上轮动；不足 max_lines 时空位留在顶部。
        rh = row_area // self.max_lines
        pad = max(3, int(rh * 0.10))

        items = list(self._items)
        n = len(items)
        if n:
            # 贴底排布：最新一条贴着 row_area 底边，往上堆叠
            start = top + row_area - n * rh
            for i, it in enumerate(items):
                age = now - it.t0
                # 入场动效：前 enter 秒从左小幅滑入（位移收敛，不推出面板）
                if age < self.enter:
                    p = age / self.enter
                    xoff = int((1 - p) * -50)
                else:
                    xoff = 0
                alpha = 255          # 保留到被顶出，不做时间淡出
                y = start + i * rh
                # 类型圆点徽章（小尺寸，不遮挡文字）
                bc = it.color + (alpha,)
                x = int(w * 0.03) + xoff
                badge_d = max(10, int(rh * 0.42))
                by = y + (rh - pad - badge_d) // 2
                d.ellipse([x, by, x + badge_d, by + badge_d], fill=bc)
                # 文本块
                box_x = x + badge_d + int(rh * 0.22)
                avail = w - box_x - int(w * 0.04)
                nick = it.nickname if len(it.nickname) <= 8 else it.nickname[:8] + "…"
                text = _fit(d, it.text, self._font_main, avail)
                # reply 高亮底：按实际文本宽度自适应
                if it.kind == "reply":
                    bw = max(d.textlength(nick, font=self._font_nick),
                             d.textlength(text, font=self._font_main)) + int(rh * 0.4)
                    d.rounded_rectangle([x - int(rh * 0.08), y,
                                         min(w - int(w * 0.025), box_x + bw),
                                         y + rh - pad],
                                        radius=max(3, int(rh * 0.16)),
                                        fill=(255, 248, 220, int(alpha * 0.16)))
                d.text((box_x, y + int(rh * 0.06)), nick, font=self._font_nick,
                       fill=bc)
                ty = y + int(rh * 0.40)
                d.text((box_x, ty), text, font=self._font_main,
                       fill=(240, 244, 250, alpha))
                if it.sub:
                    tw = d.textlength(text, font=self._font_main)
                    d.text((box_x + tw + int(rh * 0.18), ty + int(rh * 0.05)),
                           it.sub, font=self._font_small,
                           fill=(255, 180, 120, alpha))

        # 底部公告轮播
        if self.banners:
            idx = int(now // self.banner_seconds) % len(self.banners)
            b = self.banners[idx]
            col = tuple(b.get("color", (255, 200, 120))[:3]) + (235,)
            txt = _fit(d, str(b["text"]).strip(), self._font_small,
                       w - int(w * 0.08))
            by = h - bottom + int(bottom * 0.12)
            tx = (w - d.textlength(txt, font=self._font_small)) / 2
            d.text((tx, by), txt, font=self._font_small, fill=col)
        return Packet.make_video(self.channel_id, img.tobytes(), w, h, 30)


def _clip(s: str, mx: int) -> str:
    s = s or ""
    return s if len(s) <= mx else s[: mx - 1] + "…"


def _fit(d: ImageDraw.ImageDraw, s: str, font, max_px: int) -> str:
    """把文本截断到 max_px 像素宽度内（超出加省略号）。"""
    s = s or ""
    if d.textlength(s, font=font) <= max_px:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if d.textlength(s[:mid] + "…", font=font) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo] + "…" if lo > 0 else "…"
