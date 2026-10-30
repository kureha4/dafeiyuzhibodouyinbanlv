"""人物动画渲染器。

两种素材模式：
1. 外部视频（推荐，正式使用）：assets/character.*（webm/mov/mp4/mkv）。
   - 透明视频（带 Alpha）直接解码为 RGBA 帧循环推送；
   - 普通视频（无 Alpha）同样可用：默认整幅显示（不透明），
     若背景是绿幕/纯色，可打开「背景抠像」（character.chroma）在逐帧解码时
     把背景色抠成透明，效果等同透明素材。
   两种视频都用 FFmpeg 子进程管道解码（本机已有 FFmpeg 即可用；
   短视频会预解码进内存缓存，循环零成本）。
   素材缺失或解码失败时自动落到模式 2。
2. 程序化兜底动画：Pillow 直接绘制一个卡通 VTuber 形象——
   待机上下浮动+眨眼+头发/服饰飘动，说话时嘴巴张合（由 AI 层驱动 speak 状态），
   感谢礼物时鞠躬。全部像素带 Alpha 通道，透明区域即直播间背景。
"""
from __future__ import annotations

import colorsys
import math
import os
import random
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from ..ipc import Packet


def load_font(path: str, size: int):
    for p in (path, "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if p and os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


class CharacterRenderer:
    def __init__(self, cfg: dict, width: int, height: int,
                 asset_dir: str | os.PathLike | None = None) -> None:
        self.cfg = cfg or {}
        self.width = width
        self.height = height
        self.fps = 30
        self.name = (cfg.get("character") or {}).get("name", "小抖")
        self.hue = (cfg.get("character") or {}).get("base_hue", 205)
        self.idle_amp = (cfg.get("character") or {}).get("idle_amplitude", 18.0)

        # 状态（由业务层驱动）
        self._state = "idle"          # idle | speaking | thanks
        self._state_until = 0.0
        self._blink_until = 0.0
        self._next_blink = time.time() + random.uniform(2.0, 5.0)
        self._lock = threading.Lock()

        # 外部素材（透明视频或普通视频；普通视频可选背景抠像）
        self._video: VideoFrameSource | None = None
        self._last_rgba: bytes | None = None
        if asset_dir:
            ch = cfg.get("character") or {}
            chroma = ch.get("chroma") or None
            if chroma is not None and not chroma.get("enabled"):
                chroma = None
            src = VideoFrameSource.try_open(Path(asset_dir), width, height,
                                            self.fps, chroma=chroma,
                                            want=ch.get("video"))
            if src:
                self._video = src

        self._t0 = time.time()

    # -- 状态驱动 ------------------------------------------------------------
    def set_state(self, state: str, seconds: float = 2.0) -> None:
        """speaking: AI 正在说话；thanks: 感谢礼物鞠躬；idle: 待机。"""
        with self._lock:
            self._state = state
            self._state_until = time.time() + seconds

    # -- 帧生成 --------------------------------------------------------------
    def next_frame(self) -> Packet:
        if self._video:
            rgba = self._video.next_frame()
            if rgba:
                self._last_rgba = rgba
                return Packet.make_video(0, rgba, self.width, self.height, self.fps)
            # 视频已加载却偶发空帧：重发上一帧或透明帧，绝不闪默认卡通
            rgba = self._last_rgba or bytes(self.width * self.height * 4)
            return Packet.make_video(0, rgba, self.width, self.height, self.fps)
        with self._lock:
            state = self._state if time.time() < self._state_until else "idle"
            if state == "idle" and time.time() >= self._next_blink:
                self._blink_until = time.time() + 0.12
                self._next_blink = time.time() + random.uniform(2.4, 5.6)
            blink = time.time() < self._blink_until
        t = time.time() - self._t0
        img = self._draw_cartoon(t, state, blink)
        return Packet.make_video(0, img.tobytes(), self.width, self.height, self.fps)

    # -- 程序化卡通形象 ------------------------------------------------------
    def _hsl(self, s: float, l: float) -> tuple:
        r, g, b = colorsys.hls_to_rgb(self.hue / 360.0, l, s)
        return (int(r * 255), int(g * 255), int(b * 255))

    def _draw_cartoon(self, t: float, state: str, blink: bool) -> Image.Image:
        w, h = self.width, self.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        bob = math.sin(t * 2.0) * self.idle_amp
        bow = 0.0
        if state == "thanks":
            bow = abs(math.sin((t % 1.2) / 1.2 * math.pi)) * h * 0.05
        cx = w * 0.5
        head_cy = h * 0.34 + bob + bow
        head_r = w * 0.16

        body_col = self._hsl(.45, .55) + (235,)
        body_col2 = self._hsl(.5, .42) + (240,)
        hair_col = self._hsl(.55, .25) + (255,)
        skin_col = (255, 228, 205, 255)

        # 身体（圆肩）
        body_top = head_cy + head_r * 0.55
        d.ellipse([cx - w*0.22, body_top, cx + w*0.22, body_top + h*0.55],
                  fill=body_col)
        # 领子
        d.ellipse([cx - w*0.10, body_top - 6, cx + w*0.10, body_top + h*0.06],
                  fill=body_col2)
        # 手臂摆动
        swing = math.sin(t * (6 if state == "speaking" else 2.2)) * 0.25
        for side in (-1, 1):
            ax = cx + side * w * 0.19
            ay = body_top + h * 0.10
            d.ellipse([ax - w*0.055, ay, ax + w*0.055,
                       ay + h*0.18 + swing*side*h*0.02], fill=body_col)

        # 后发
        d.ellipse([cx - head_r*1.25, head_cy - head_r*1.1,
                   cx + head_r*1.25, head_cy + head_r*1.6], fill=hair_col)
        # 脸
        d.ellipse([cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
                  fill=skin_col)
        # 刘海
        d.pieslice([cx - head_r*1.15, head_cy - head_r*1.25,
                    cx + head_r*1.15, head_cy + head_r*0.2],
                   180, 360, fill=hair_col)
        sway = math.sin(t * 1.7) * head_r * 0.05
        for side in (-1, 1):
            d.polygon([(cx + side*head_r*0.95 + sway, head_cy - head_r*0.2),
                       (cx + side*head_r*1.25, head_cy + head_r*0.9),
                       (cx + side*head_r*0.62, head_cy + head_r*0.75)],
                      fill=hair_col)

        # 眼睛
        eye_dx, eye_dy = head_r * 0.42, head_r * 0.08
        for side in (-1, 1):
            ex = cx + side * eye_dx
            ey = head_cy + eye_dy
            if blink:
                d.line([ex - head_r*0.18, ey, ex + head_r*0.18, ey],
                       fill=(60, 45, 40, 255), width=max(2, int(head_r*0.05)))
            else:
                d.ellipse([ex - head_r*0.17, ey - head_r*0.22,
                           ex + head_r*0.17, ey + head_r*0.22],
                          fill=(45, 35, 55, 255))
                d.ellipse([ex - head_r*0.07, ey - head_r*0.12,
                           ex + head_r*0.05, ey - head_r*0.0],
                          fill=(255, 255, 255, 230))
        # 腮红
        for side in (-1, 1):
            d.ellipse([cx + side*head_r*0.62 - head_r*0.12, head_cy + head_r*0.30,
                       cx + side*head_r*0.62 + head_r*0.12, head_cy + head_r*0.46],
                      fill=(255, 150, 150, 110))
        # 嘴
        mouth_cy = head_cy + head_r * 0.55
        if state == "speaking":
            open_amt = (0.5 + 0.5 * math.sin(t * 14)) * head_r * 0.18 + head_r * 0.04
            d.ellipse([cx - head_r*0.16, mouth_cy - open_amt*0.5,
                       cx + head_r*0.16, mouth_cy + open_amt],
                      fill=(170, 60, 70, 255))
        else:
            d.arc([cx - head_r*0.2 if state == "thanks" else cx - head_r*0.13,
                   mouth_cy - head_r*0.12 if state == "thanks" else mouth_cy - head_r*0.06,
                   cx + head_r*0.2 if state == "thanks" else cx + head_r*0.13,
                   mouth_cy + head_r*0.16 if state == "thanks" else mouth_cy + head_r*0.10],
                  20, 160, fill=(170, 60, 70, 255),
                  width=max(3 if state == "thanks" else 2, int(head_r*0.05)))

        # 名字铭牌（半透明圆角）
        tag_h = h * 0.045
        tag_y = body_top + h * 0.30
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rounded_rectangle([cx - w*0.12, tag_y, cx + w*0.12, tag_y + tag_h],
                             radius=tag_h/2, fill=(10, 14, 26, 150))
        try:
            font = load_font(self.cfg.get("font_path", ""), int(tag_h*0.62))
            od.text((cx, tag_y + tag_h*0.5), self.name, font=font,
                    fill=(255, 255, 255, 235), anchor="mm")
        except Exception:
            pass
        return Image.alpha_composite(img, overlay)


# ---------------------------------------------------------------------------
# 外部视频素材源（FFmpeg 子进程解码，透明/普通视频通用）
# ---------------------------------------------------------------------------
class VideoFrameSource:
    """把透明或普通视频解码成 RGBA 帧循环。

    - 带 Alpha 的透明视频（webm/vp9、mov/prores4444 等）解码后直接使用；
    - 普通视频（mp4/h264 等无 Alpha）解码为完全不透明帧；若传入 chroma 配置
      （{"color":[r,g,b], "tolerance":60, "softness":30}），则逐帧把接近该颜色的
      背景抠成透明，效果等同透明素材。
    短视频优先预解码进内存缓存（估算 <= cache_limit_mb），否则实时流解码、
    解码结束自动重开进程实现循环。
    """

    def __init__(self, path: Path, width: int, height: int, fps: int,
                 cache_limit_mb: int = 400, chroma: dict | None = None):
        self.path = path
        self.width, self.height, self.fps = width, height, fps
        self._frames: list[bytes] = []
        self._idx = 0
        self._proc: subprocess.Popen | None = None
        self._last: bytes | None = None
        self._lock = threading.Lock()

        # 抠像：只对无 Alpha 的普通视频生效（带 Alpha 的素材不需要、也不该抠）
        self.has_alpha = self._probe_alpha()
        self._chroma = chroma if (chroma and not self.has_alpha) else None
        self._luts = self._build_luts() if self._chroma else None
        if chroma and self.has_alpha:
            print(f"[character] {path.name} 自带 Alpha，已忽略「背景抠像」设置")

        dur = self._probe_duration()
        approx_mb = dur * fps * width * height * 4 / 1024 / 1024 if dur else 0
        if dur and approx_mb <= cache_limit_mb:
            self._prefetch_all()
        else:
            self._open_stream()

    @classmethod
    def try_open(cls, asset_dir: Path, width: int, height: int, fps: int,
                 chroma: dict | None = None, want: str | None = None):
        """打开人物素材：优先配置指定的视频文件，否则按 character* 扫描。"""
        if not asset_dir.exists():
            return None
        cands: list[Path] = []
        if want:                       # 防路径穿越：只取文件名部分
            cands.append(asset_dir / Path(str(want)).name)
        for ext in (".webm", ".mov", ".mp4", ".mkv"):
            for f in sorted(asset_dir.glob(f"character*{ext}")):
                if f not in cands:
                    cands.append(f)
        for f in cands:
            if not f.exists() or not f.is_file():
                continue
            try:
                return cls(f, width, height, fps, chroma=chroma)
            except Exception as e:
                print(f"[character] 素材 {f.name} 打开失败: {e}")
        return None

    def _probe_duration(self) -> float:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(self.path)],
                capture_output=True, text=True, timeout=10)
            return float(out.stdout.strip())
        except Exception:
            return 0.0

    def _probe_alpha(self) -> bool:
        """探测视频是否自带 Alpha（pix_fmt 含 a 即带透明通道）。"""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=pix_fmt", "-of", "csv=p=0",
                 str(self.path)],
                capture_output=True, text=True, timeout=10)
            pix = out.stdout.strip().lower()
            return pix.startswith(("ya", "rgba", "argb", "bgra", "abgr",
                                   "gbrap", "yuva"))
        except Exception:
            return False

    def _build_luts(self) -> tuple:
        """按抠像颜色生成三条 0-255 通道 LUT（255=背景，0=画面，中间为边缘过渡）。"""
        color = (self._chroma.get("color") or [0, 255, 0])
        tol = max(0, min(255, int(self._chroma.get("tolerance", 60))))
        soft = max(0, min(255, int(self._chroma.get("softness", 30))))
        key = [int(c) for c in (list(color) + [0, 255, 0])[:3]]
        luts = []
        for kv in key:
            lut = []
            for v in range(256):
                d = abs(v - kv)
                if d <= tol:
                    a = 255
                elif soft > 0 and d < tol + soft:
                    a = int(255 * (tol + soft - d) / soft)
                else:
                    a = 0
                lut.append(a)
            luts.append(lut)
        return tuple(luts)

    def _apply_chroma(self, rgba: bytes) -> bytes:
        im = Image.frombytes("RGBA", (self.width, self.height), rgba)
        r, g, b, a = im.split()
        lr, lg, lb = self._luts
        inside = ImageChops.darker(ImageChops.darker(r.point(lr), g.point(lg)),
                                   b.point(lb))
        a2 = ImageChops.multiply(a, ImageChops.invert(inside))
        im.putalpha(a2)
        return im.tobytes()

    def _decode_cmd(self) -> list[str]:
        return ["ffmpeg", "-v", "error", "-i", str(self.path),
                "-vf",
                f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
                f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2",
                "-pix_fmt", "rgba", "-f", "rawvideo", "-"]

    def _prefetch_all(self) -> None:
        proc = subprocess.Popen(self._decode_cmd(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        assert proc.stdout
        fb = self.width * self.height * 4
        buf = b""
        while True:
            chunk = proc.stdout.read(fb - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) == fb:
                self._frames.append(buf)
                buf = b""
        proc.wait()
        if not self._frames:
            raise RuntimeError("未解码到任何帧")

    def _open_stream(self) -> None:
        self._proc = subprocess.Popen(self._decode_cmd(), stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)

    def next_frame(self) -> bytes | None:
        if self._frames:
            with self._lock:
                f = self._frames[self._idx]
                self._idx = (self._idx + 1) % len(self._frames)
            return self._apply_chroma(f) if self._chroma else f
        if self._proc and self._proc.stdout:
            got = self._proc.stdout.read(self.width * self.height * 4)
            if len(got) == self.width * self.height * 4:
                out = self._apply_chroma(got) if self._chroma else got
                self._last = out
                return out
            # 播放完毕：重开进程循环。当帧返回上一帧（不留空窗，
            # 否则渲染层会闪出默认卡通动画）
            self._open_stream()
            return self._last
        return self._last

    def close(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
