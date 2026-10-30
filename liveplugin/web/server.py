"""Web 控制台服务器：给命令行插件套一层浏览器 UI。

纯 Python 标准库实现（http.server + SSE + MJPEG），不引入任何新依赖，
与项目「零重型依赖」的风格一致。

架构：
    Browser(静态页) ── fetch JSON API ─┐
                 ── EventSource(SSE) ──┼─> ThreadingHTTPServer
                 ── <img>/mjpeg ───────┘        │
                                               v
    WebConsole ── ConsoleApp(LivePluginApp)     渲染循环照常 30fps 推帧
              ├─ Bus（事件/回复/日志 → SSE）
              ├─ WebPreview（两通道帧 → JPEG → MJPEG）
              ├─ WssManager（运行时连接/断开 WSS 逆向源）
              └─ SimManager（模拟互动消息开关，等效调试面板按钮）

启动：python -m liveplugin.web --port 8765
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..ai import AIOrchestrator
from ..config import Config, _deep_merge
from ..events import LiveEvent
from ..main import LivePluginApp, Simulator, _asset_dir
from ..audiospectrum import SpectrumSource
from ..render import (BalanceRenderer, BubbleRenderer, CharacterRenderer,
                      DanmakuRenderer, HistoRenderer, SongQueueRenderer)
from ..wss_source import DouyinWssSource
from .bus import Bus, sse_encode

_STATIC = Path(__file__).resolve().parent / "static"

# 预览画布：把两个素材通道缩放到浏览器里看得清的横向布局


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def ev_to_dict(ev: LiveEvent) -> dict:
    d = {"type": ev.type, "nickname": ev.nickname, "ts": ev.timestamp,
         "content": ev.content, "gift_name": ev.gift_name,
         "gift_num": ev.gift_num, "like_num": ev.like_num,
         "follow_action": ev.follow_action, "member_count": ev.member_count,
         "fansclub_level": ev.fansclub_level, "msg_id": ev.msg_id}
    if ev.type == "room_stats":
        d["online"] = int(ev.raw.get("online_user_count", 0) or 0)
    return d


# ---------------------------------------------------------------------------
# 预览合成器：两通道 RGBA 帧 → 画布 → JPEG（供 MJPEG / 快照）
# ---------------------------------------------------------------------------
class WebPreview:
    """最终成品画布：按 config.canvas 全分辨率合成三路素材，所见即所得。

    不再摆缩略图——画布本身就是直播画面（用屏幕捕捉这一路即可上播）。
    """

    def __init__(self, cfg, character_id: int, danmaku_id: int,
                 balance_id: int = 3, songq_id: int = 4,
                 bubble_id: int = 5, histo_id: int = 6) -> None:
        from PIL import Image
        self._Image = Image
        self.cfg = cfg                      # Config 对象，每帧热读 canvas 配置
        self.character_id = character_id
        self.danmaku_id = danmaku_id
        self.balance_id = balance_id
        self._keys = {character_id: "character", danmaku_id: "danmaku",
                      balance_id: "balance", songq_id: "songq",
                      bubble_id: "bubble", histo_id: "histo"}
        self._pending: dict[int, tuple[int, int, bytes]] = {}
        self._lock = threading.Lock()
        self._jpeg: bytes = b""
        self._seq = 0
        self._bg_cache: tuple[str, object] = ("", None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="web-preview")

    def feed(self, pkt) -> None:
        if pkt.video is None:
            return
        with self._lock:
            self._pending[pkt.channel_id] = (pkt.video.cx, pkt.video.cy,
                                             pkt.data)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[int, bytes]:
        with self._lock:
            return self._seq, self._jpeg

    def _bg(self, cv: dict, W: int, H: int):
        Image = self._Image
        name = str(cv.get("bg_image") or "")
        if name:
            key = name
            if self._bg_cache[0] != key or self._bg_cache[1] is None:
                p = self._assets_dir() / name
                img = None
                try:
                    img = Image.open(p).convert("RGBA").resize((W, H))
                except Exception:
                    img = None
                self._bg_cache = (key, img)
            if self._bg_cache[1] is not None:
                return self._bg_cache[1].copy()
        bg = tuple(cv.get("bg", (12, 16, 30)))[:3]
        return Image.new("RGBA", (W, H), tuple(bg) + (255,))

    @staticmethod
    def _assets_dir() -> Path:
        return Path(__file__).resolve().parent.parent.parent / "assets"

    def _loop(self) -> None:
        while True:
            cv = dict(self.cfg.get("canvas") or {})
            fps = max(6, min(30, int(cv.get("fps", 24))))
            if self._stop.wait(1.0 / fps):
                return
            with self._lock:
                frames = dict(self._pending)
            if not frames:
                continue
            try:
                W = int(cv.get("width", 1080))
                H = int(cv.get("height", 1920))
                canvas = self._bg(cv, W, H)
                layout = cv.get("layout") or {}
                # 固定叠放顺序（人物→弹幕墙→余额→队列→频谱→气泡在最上层），
                # 不依赖帧到达先后，否则上层素材可能被人物视频盖住
                for ch_id, key in sorted(
                        ((c, k) for c, k in self._keys.items()),
                        key=lambda t: ("character", "danmaku", "balance",
                                       "songq", "histo", "bubble").index(t[1])):
                    if ch_id not in frames:
                        continue
                    cx, cy, data = frames[ch_id]
                    box = layout.get(key) or {}
                    iw = int(box.get("w", cx))
                    ih = int(box.get("h", cy))
                    img = self._Image.frombytes("RGBA", (cx, cy), data)
                    if (iw, ih) != (cx, cy):
                        img = img.resize((iw, ih), self._Image.LANCZOS)
                    canvas.alpha_composite(img, (int(box.get("x", 0)),
                                                 int(box.get("y", 0))))
                buf = io.BytesIO()
                canvas.convert("RGB").save(
                    buf, "JPEG", quality=int(cv.get("quality", 88)))
                with self._lock:
                    self._jpeg = buf.getvalue()
                    self._seq += 1
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WSS 逆向源运行时管理（可反复连接/断开）
# ---------------------------------------------------------------------------
class WssManager:
    def __init__(self, bus: Bus, on_payload) -> None:
        self.bus = bus
        self.on_payload = on_payload
        self.source: DouyinWssSource | None = None
        self.status = "idle"                 # idle / connecting / connected / closed / error
        self.params: dict = {}               # 上次连接参数（敏感字段只在内存）
        self.last_error = ""
        self._lock = threading.Lock()

    @staticmethod
    def _anon_ttwid() -> str:
        """向字节匿名注册接口申领 ttwid（已验证可用，无账号风险）。"""
        import urllib.request
        try:
            body = json.dumps({
                "region": "cn", "aid": 6383, "needFid": False,
                "service": "www.douyin.com",
                "migrate_info": {"ticket": "", "source": "node"},
                "cbUrlProtocol": "https", "union": True}).encode()
            req = urllib.request.Request(
                "https://ttwid.bytedance.com/ttwid/union/register/", body,
                {"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
                sc = r.headers.get("Set-Cookie", "")
            if "ttwid=" in sc:
                return sc.split("ttwid=")[1].split(";")[0]
        except Exception as e:
            print(f"[wss] 自动申领 ttwid 失败: {e!r}")
        return ""

    def connect(self, p: dict) -> dict:
        mode = str(p.get("mode", "") or "")
        if not mode and p.get("room_url") and not str(p.get("room_id", "")).strip():
            mode = "auto"                      # config 里存的是链接模式
        with self._lock:
            if self.status in ("connecting", "connected"):
                return {"ok": False, "message": f"已处于 {self.status} 状态，先断开"}
            if mode == "auto":               # 直通模式：只填直播间链接
                room_url = str(p.get("room_url", "")).strip()
                if not room_url:
                    return {"ok": False, "message": "请输入直播间链接或直播间号"}
                self.params = {"room_id": "", "room_url": room_url}
                self.status = "connecting"
                self.last_error = ""
            else:
                room_id = str(p.get("room_id", "")).strip()
                if not room_id:
                    return {"ok": False, "message": "room_id 不能为空"}
                self.params = {k: str(p.get(k, "") or "") for k in
                               ("room_id", "user_id", "device_id", "cookie",
                                "ttwid", "signature", "host", "ws_url")}
                self.status = "connecting"
                self.last_error = ""
        if mode == "auto":
            return self._connect_cdp()
        if not self.params["ttwid"]:
            auto = self._anon_ttwid()
            if auto:
                self.params["ttwid"] = auto
                self.bus.log("ttwid 未提供，已自动申领匿名 ttwid")

        src = DouyinWssSource(self.on_payload, verbose=True,
                              log=lambda *a: self._log(" ".join(str(x) for x in a)))
        # 子类化钩子改为状态机跟踪
        orig_open, orig_err, orig_close = src._on_open, src._on_error, src._on_close

        def on_open(ws):
            self.status = "connected"
            self.bus.log(f"wss 已连接 room_id={self.params['room_id']}", "ok")
            orig_open(ws)

        def on_error(ws, err):
            self.status = "error"
            self.last_error = str(err)
            self.bus.log(f"wss 错误: {err}", "error")
            orig_err(ws, err)

        def on_close(ws, code, msg):
            if self.status != "error":
                self.status = "closed"
            self.bus.log(f"wss 断开 code={code}（检查鉴权是否过期/协议变更）", "warn")
            orig_close(ws, code, msg)

        src._on_open, src._on_error, src._on_close = on_open, on_error, on_close
        try:
            src.connect(self.params["room_id"],
                        user_id=self.params["user_id"] or "0",
                        device_id=self.params["device_id"],
                        cookie=self.params["cookie"],
                        ttwid=self.params["ttwid"],
                        signature=self.params["signature"],
                        host=self.params["host"] or
                        "webcast5-ws-web-lf.douyin.com",
                        url=self.params.get("ws_url", ""))
        except Exception as e:
            self.status = "error"
            self.last_error = repr(e)
            self.bus.log(f"wss 启动失败: {e}", "error")
            return {"ok": False, "message": repr(e)}
        self.source = src
        self.bus.log(f"wss 连接请求已发出 room_id={room_id}")
        return {"ok": True}

    def _connect_cdp(self) -> dict:
        """直通模式：匿名浏览器实例打开直播间，转发页内已鉴权 WS 的帧。
        用户只需粘贴直播间链接，无 F12、无参数、无 Cookie。"""
        from ..wss_auto import CdpWssSource
        src = CdpWssSource(self.on_payload, verbose=True,
                           log=lambda *a: self._log(" ".join(str(x) for x in a)))
        orig_open, orig_err, orig_close = src._on_open, src._on_error, src._on_close

        def on_open(ws):
            self.status = "connected"
            self.bus.log("wss 已连接（直通模式：浏览器实例维持鉴权）", "ok")
            orig_open(ws)

        def on_error(ws, err):
            self.status = "error"
            self.last_error = str(err)
            self.bus.log(f"wss 错误: {err}", "error")
            orig_err(ws, err)

        def on_close(ws, code, msg):
            if self.status != "error":
                self.status = "closed"
            self.bus.log("wss 连接结束（直播结束或浏览器实例退出）", "warn")
            orig_close(ws, code, msg)

        src._on_open, src._on_error, src._on_close = on_open, on_error, on_close
        try:
            src.connect(self.params["room_url"])
        except Exception as e:
            self.status = "error"
            self.last_error = repr(e)
            self.bus.log(f"直通启动失败: {e}", "error")
            return {"ok": False, "message": repr(e)}
        self.source = src
        self.bus.log("直通模式启动：正在用匿名浏览器打开直播间（约 10~30 秒）…")
        return {"ok": True}

    def disconnect(self) -> dict:
        with self._lock:
            src, self.source = self.source, None
            if self.status in ("idle", "closed", "error"):
                self.status = "idle"
                return {"ok": True, "message": "本来就没有连接"}
        if src:
            src.close()
        self.status = "idle"
        self.bus.log("wss 已手动断开")
        return {"ok": True}

    def _log(self, text: str) -> None:
        # 把 wss_source 内部 print 的噪声也汇入日志面板
        if text.startswith("[wss]"):
            self.bus.log(text[5:].strip(), "wss")

    def info(self) -> dict:
        stats = self.source.stats if self.source else {}
        return {"status": self.status,
                "room_id": (self.params.get("room_id")
                            or getattr(self.source, "room_id", "") or ""),
                "last_error": self.last_error, "stats": stats}


# ---------------------------------------------------------------------------
# 带总线挂钩的 App：事件/回复同时进 SSE
# ---------------------------------------------------------------------------
class ConsoleApp(LivePluginApp):
    def __init__(self, cfg: Config, bus: Bus, **kw) -> None:
        self.bus = bus
        super().__init__(cfg, **kw)

    def _on_live_event(self, ev: LiveEvent) -> None:
        super()._on_live_event(ev)
        self.bus.publish("event", ev_to_dict(ev))

    def _on_reply(self, text: str, ev: LiveEvent) -> None:
        super()._on_reply(text, ev)
        self.bus.publish("reply", {"text": text, "to": ev.nickname,
                                   "type": ev.type, "ts": time.time()})


# ---------------------------------------------------------------------------
# 控制台：装配全部组件
# ---------------------------------------------------------------------------
class WebConsole:
    def __init__(self, cfg_path: str | None = None,
                 pipe_name: str | None = None, max_channels: int = 6,
                 mate_version: str = "0", layout_mode: int = 0,
                 use_mock: bool = True) -> None:
        self.cfg = Config.load(cfg_path)
        self.cfg_path = cfg_path or str(
            Path(__file__).resolve().parent.parent.parent / "config.json")
        self.bus = Bus()
        self.app = ConsoleApp(self.cfg, self.bus,
                              pipe_name=pipe_name, max_channels=max_channels,
                              mate_version=mate_version, layout_mode=layout_mode,
                              use_mock=use_mock)
        ch = self.cfg["channels"]
        self.preview = WebPreview(self.cfg, ch["character"]["id"],
                                  ch["danmaku"]["id"],
                                  (ch.get("balance") or {}).get("id", 3),
                                  (ch.get("songq") or {}).get("id", 4),
                                  (ch.get("bubble") or {}).get("id", 5),
                                  (ch.get("histo") or {}).get("id", 6))
        self.app.jukebox.log = self.bus.log
        self.app.tts._log = self.bus.log   # TTS 失败/预热信息进控制台日志
        self.app.pipe.set_frame_sink(self.preview.feed)

        self.wss = WssManager(
            self.bus,
            lambda payload: self.app.events.handle_partner_event(
                "OPEN_LIVE_DATA", {"payload": payload}))

        self.sim: Simulator | None = None
        self._sim_interval = 1.8
        self._inject_seq = 0
        self._srv: ThreadingHTTPServer | None = None
        self._app_thread: threading.Thread | None = None
        self._quit = threading.Event()

    # -- 生命周期 -------------------------------------------------------------
    def start_app(self) -> None:
        """启动渲染主循环（与 CLI 的 main 相同的装配，跑在后台线程）。"""
        self.preview.start()
        self._app_thread = threading.Thread(target=self.app.run, args=(0,),
                                            daemon=True, name="app-loop")
        self._app_thread.start()
        # 预填 WSS 表单：config.json 里已有的非敏感参数
        saved = self.cfg.get("wss") or {}
        for k in ("room_id", "user_id", "device_id"):
            if saved.get(k) and not self.wss.params.get(k):
                self.wss.params[k] = str(saved[k])
        if saved.get("autostart") and (saved.get("room_url")
                                       or saved.get("room_id")):
            threading.Timer(1.0, lambda: self.wss.connect(dict(saved))).start()
            self.bus.log("检测到 wss.autostart=true，正在自动连接…")
        # 人物视频队列轮换：每 rotate_seconds 秒随机切到库里另一条视频
        self._vrot_last = time.time()
        threading.Thread(target=self._video_rotate_loop, daemon=True,
                         name="video-rotate").start()

    def _video_rotate_loop(self) -> None:
        """人物视频随机切换（render.character.rotate_seconds，0=不切）。
        库里有 ≥2 条视频才切；切完热重建渲染器，从新视频第一帧开始播。"""
        import random as _rnd
        while not self._quit.is_set():
            time.sleep(5)
            try:
                chcfg = self.cfg.data["render"].get("character") or {}
                rs = int(chcfg.get("rotate_seconds", 90) or 0)
                if rs <= 0 or self._quit.is_set():
                    continue
                vids = self._videos_list()
                if len(vids) < 2 or time.time() - self._vrot_last < rs:
                    continue
                pick = _rnd.choice([v for v in vids
                                    if v != chcfg.get("video")])
                chcfg["video"] = pick
                self.save_config({})
                self._rebuild_renderers()
                self._vrot_last = time.time()
                self.bus.log(f"人物视频随机切换 → assets/{pick}（每 {rs}s 轮换）")
            except Exception as e:
                self.bus.log(f"视频轮换异常: {e!r}", "error")

    def stop(self) -> None:
        if self.sim:
            self.sim.stop()
            self.sim = None
        self.wss.disconnect()
        self.preview.stop()
        self.app.shutdown()

    # -- 模拟互动消息 ---------------------------------------------------------
    def sim_start(self, interval: float) -> dict:
        if self.sim:
            return {"ok": False, "message": "模拟器已在运行"}
        self._sim_interval = max(0.2, float(interval))
        self.sim = Simulator(
            lambda payload: self.app.pipe.inject_event(  # type: ignore[attr-defined]
                "OPEN_LIVE_DATA", {"payload": payload}),
            interval=self._sim_interval)
        self.sim.start()
        self.bus.log(f"模拟互动消息已开启（间隔 {self._sim_interval:g}s）")
        return {"ok": True}

    def sim_stop(self) -> dict:
        if not self.sim:
            return {"ok": False, "message": "模拟器未在运行"}
        self.sim.stop()
        self.sim = None
        self.bus.log("模拟互动消息已停止")
        return {"ok": True}

    # -- 手动注入单条事件（等效调试面板手发一条） --------------------------------
    _INJECT_TYPES = {"comment": 2, "gift": 3, "like": 1, "follow": 5, "member": 6}

    def inject_event(self, p: dict) -> dict:
        etype = p.get("type", "comment")
        if etype not in self._INJECT_TYPES:
            return {"ok": False, "message": f"未知类型 {etype}"}
        self._inject_seq += 1
        base = {"msg_id": f"ui-{int(time.time())}-{self._inject_seq}",
                "timestamp": int(time.time() * 1000),
                "sec_open_id": p.get("user_id") or "u_ui_0001",
                "nickname": p.get("nickname") or "控制台观众",
                "avatar_url": "", "user_privilege_level": 1,
                "is_follow_anchor": False, "fansclub_level": 0}
        if etype == "comment":
            base.update(msg_type=2, msg_type_str="live_comment",
                        content=str(p.get("content", "")))
        elif etype == "gift":
            base.update(msg_type=3, msg_type_str="live_gift",
                        gift_name_plain=p.get("gift_name", "小心心"),
                        gift_num=max(1, int(p.get("num", 1))))
        elif etype == "like":
            base.update(msg_type=1, msg_type_str="live_like",
                        like_num=max(1, int(p.get("num", 10))))
        elif etype == "follow":
            base.update(msg_type=5, msg_type_str="live_follow",
                        user_follow_action=1)
        elif etype == "member":
            base.update(msg_type=6, msg_type_str="live_member",
                        member_count=int(p.get("member_count", 1)))
        self.app.pipe.inject_event("OPEN_LIVE_DATA", {"payload": [base]})  # type: ignore[attr-defined]
        return {"ok": True}

    # -- AI 配置热更新 ---------------------------------------------------------
    _AI_EDITABLE = ("enabled", "persona", "welcome_template", "gift_template",
                    "model", "base_url", "temperature", "max_reply_chars",
                    "min_interval", "history_size", "request_timeout",
                    "max_tokens", "reply_types")

    def update_ai(self, p: dict) -> dict:
        ai: AIOrchestrator = self.app.ai
        changed = []
        for k in self._AI_EDITABLE:
            if k in p:
                ai.cfg[k] = p[k]
                changed.append(k)
        # 立即生效的字段直接重刷到运行中的编排器
        if "enabled" in p:
            ai.enabled = bool(p["enabled"])
        if "persona" in p:
            ai.persona = str(p["persona"])
        if "welcome_template" in p:
            ai.welcome_tpl = str(p["welcome_template"])
        if "gift_template" in p:
            ai.gift_tpl = str(p["gift_template"])
        if "min_interval" in p and isinstance(p["min_interval"], dict):
            ai.min_interval = {k: float(v) for k, v in p["min_interval"].items()}
        if "max_reply_chars" in p:
            ai.max_reply_chars = int(p["max_reply_chars"])
        if "temperature" in p:
            ai.temperature = float(p["temperature"])
        if "api_key" in p and str(p["api_key"]).strip():
            ai.cfg["api_key"] = str(p["api_key"]).strip()
        self.cfg.data["ai"].update({k: v for k, v in p.items()
                                    if k in self._AI_EDITABLE or k == "api_key"})
        self.bus.log(f"AI 配置已更新: {', '.join(changed) or '（无变更字段）'}")
        return {"ok": True}

    # -- 装修实时热应用 --------------------------------------------------------
    def apply_render(self, p: dict) -> dict:
        """把装修参数合并进配置并重建渲染器（预览立刻变化）。"""
        if isinstance(p.get("danmaku"), dict):
            self.cfg.data["render"]["danmaku"] = _deep_merge(
                self.cfg.data["render"].get("danmaku", {}), p["danmaku"])
        if isinstance(p.get("character"), dict):
            self.cfg.data["render"]["character"] = _deep_merge(
                self.cfg.data["render"].get("character", {}), p["character"])
        if isinstance(p.get("balance"), dict):
            self.cfg.data["render"]["balance"] = _deep_merge(
                self.cfg.data["render"].get("balance", {}), p["balance"])
        if isinstance(p.get("songq"), dict):
            self.cfg.data["render"]["songq"] = _deep_merge(
                self.cfg.data["render"].get("songq", {}), p["songq"])
        if isinstance(p.get("bubble"), dict):
            self.cfg.data["render"]["bubble"] = _deep_merge(
                self.cfg.data["render"].get("bubble", {}), p["bubble"])
        if isinstance(p.get("histo"), dict):
            self.cfg.data["render"]["histo"] = _deep_merge(
                self.cfg.data["render"].get("histo", {}), p["histo"])
        if isinstance(p.get("sfx"), dict):
            self.cfg.data["sfx"] = _deep_merge(
                self.cfg.data.get("sfx") or {}, p["sfx"])
        if isinstance(p.get("canvas"), dict):
            self.cfg.data["canvas"] = _deep_merge(
                self.cfg.data.get("canvas", {}), p["canvas"])
            # 关键：通道渲染分辨率 = 画布上的显示大小，文字/视频按实际尺寸渲染（不糊）
            lay = self.cfg.data["canvas"].get("layout") or {}
            for key, box in lay.items():
                chn = (self.cfg.data.get("channels") or {}).get(key)
                if isinstance(chn, dict) and isinstance(box, dict):
                    if "w" in box:
                        chn["width"] = max(64, min(1920, int(box["w"] or 0)))
                    if "h" in box:
                        chn["height"] = max(64, min(1920, int(box["h"] or 0)))
        self._rebuild_renderers()
        self.save_config({})          # 实时应用即保存，重启后不丢
        self.bus.log("面板装修已实时应用并保存")
        return {"ok": True}

    def _rebuild_renderers(self) -> None:
        ch = self.cfg["channels"]
        r = self.cfg["render"]
        old = self.app.danmaku
        new = DanmakuRenderer(dict(r["danmaku"], font_path=r["font_path"]),
                              ch["danmaku"]["width"], ch["danmaku"]["height"],
                              channel_id=ch["danmaku"]["id"])
        try:
            new._items = old._items          # 保留已上墙的弹幕，切换不闪断
        except Exception:
            pass
        self.app.danmaku = new
        try:
            snap = self.app.ledger.snapshot()
            new.set_balance(snap["balance"], snap.get("currency"))
        except Exception:
            pass
        bc = self.cfg["channels"].get("balance") or {}
        self.app.balance = BalanceRenderer(
            dict(r.get("balance") or {}, font_path=r["font_path"]),
            int(bc.get("width", 520)), int(bc.get("height", 170)),
            channel_id=int(bc.get("id", 3)))
        self.app.balance.set_source(lambda: self.app.ledger.snapshot())
        sc = self.cfg["channels"].get("songq") or {}
        self.app.songq = SongQueueRenderer(
            dict(r.get("songq") or {}, font_path=r["font_path"]),
            int(sc.get("width", 520)), int(sc.get("height", 300)),
            channel_id=int(sc.get("id", 4)))
        self.app.songq.set_source(lambda: self.app.jukebox.panel())
        pc = self.cfg["channels"].get("bubble") or {}
        old_b = getattr(self.app, "bubble", None)
        self.app.bubble = BubbleRenderer(
            dict(r.get("bubble") or {}, font_path=r["font_path"]),
            int(pc.get("width", 620)), int(pc.get("height", 190)),
            channel_id=int(pc.get("id", 5)))
        if old_b is not None:               # 装修热重建时当前气泡不闪断
            with old_b._lock:
                self.app.bubble._text, self.app.bubble._t0 = (old_b._text,
                                                              old_b._t0)
        # 「音乐频谱」重建 + 频谱采集线程随开关起停
        hc = self.cfg["channels"].get("histo") or {}
        hcfg = dict(r.get("histo") or {})
        self.app.histo = HistoRenderer(hcfg, int(hc.get("width", 460)),
                                       int(hc.get("height", 140)),
                                       channel_id=int(hc.get("id", 6)))
        sp = getattr(self.app, "spectrum", None)
        if hcfg.get("enabled", True):
            if sp is None:
                sp = SpectrumSource(bars=int(hcfg.get("bars", 28) or 28),
                                    sensitivity=float(hcfg.get("sensitivity",
                                                                1.0) or 1.0))
                self.app.spectrum = sp
            else:
                sp.set_bars(int(hcfg.get("bars", 28) or 28))
                sp.sensitivity = float(hcfg.get("sensitivity", 1.0) or 1.0)
            self.app.histo.set_source(sp.levels)
        else:
            if sp is not None:
                sp.stop()
                self.app.spectrum = None
        self.app.character = CharacterRenderer(
            dict(r, fps=self.cfg["fps"]), ch["character"]["width"],
            ch["character"]["height"], asset_dir=self._assets_dir())
        self.app.ai.cfg["char_name"] = r["character"].get("name", "小抖")

    def upload(self, p: dict) -> dict:
        """素材上传（JSON base64）：图片→背景图；视频→人物素材；wav→礼物音效。"""
        import base64
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9_.-]", "", str(p.get("name", "")))
        if not name or name.startswith("."):
            return {"ok": False, "message": "非法文件名"}
        ext = Path(name).suffix.lower()
        is_video = ext in (".mp4", ".webm", ".mov", ".mkv")
        is_image = ext in (".png", ".jpg", ".jpeg", ".webp")
        is_wav = ext == ".wav"
        if not (is_video or is_image or is_wav):
            return {"ok": False,
                    "message": "只支持图片 png/jpg/webp、视频 mp4/webm/mov/mkv "
                               "或音效 wav"}
        try:
            raw = base64.b64decode(str(p.get("b64", "")))
        except Exception:
            return {"ok": False, "message": "数据解码失败"}
        limit = 48 * 1024 * 1024 if is_video else 8 * 1024 * 1024
        if not 0 < len(raw) <= limit:
            return {"ok": False, "message": f"文件为空或超过 {limit // 1024 // 1024}MB"}
        assets = self._assets_dir()
        assets.mkdir(parents=True, exist_ok=True)
        if is_video:
            return self._install_character_video(assets, ext, raw)
        if is_wav:
            return self._install_gift_sfx(assets, name, raw)
        fp = assets / name
        fp.write_bytes(raw)
        from PIL import Image as _I
        try:
            _I.open(fp).load()
        except Exception as e:
            fp.unlink()
            return {"ok": False, "message": f"图片无法解析: {e}"}
        self.bus.log(f"背景图已上传: assets/{name}（{len(raw) // 1024}KB）")
        return {"ok": True, "path": f"assets/{name}"}

    def _install_gift_sfx(self, assets: Path, name: str, raw: bytes) -> dict:
        """安装礼物音效 wav 并加进音效列表（多个随机挑一个播）。"""
        fp = assets / name
        fp.write_bytes(raw)
        sfx = self.cfg.data.setdefault("sfx", {"enabled": True, "files": []})
        files = sfx.setdefault("files", [])
        if name not in files:
            files.append(name)
        self.save_config({})
        self.bus.log(f"礼物音效已安装: assets/{name}（{len(raw) // 1024}KB）")
        played = self.app.sfx.play()      # 装好当场试听一声，确认响
        return {"ok": True, "path": f"assets/{name}", "files": files,
                "played": played}

    def _install_character_video(self, assets: Path, ext: str, raw: bytes) -> dict:
        """校验并安装人物视频（透明或普通视频），装好即热重建渲染器。"""
        import subprocess
        tmp = assets / f"._upload{ext}"
        dur = 0.0
        try:
            tmp.write_bytes(raw)
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration", "-of", "csv=p=0", str(tmp)],
                    capture_output=True, text=True, timeout=20)
                dur = float((out.stdout or "").strip() or 0)
            except Exception:
                dur = 0.0
            if dur <= 0:
                return {"ok": False,
                        "message": "视频无法解析：本机需要安装 FFmpeg（并加入 PATH）"}
            # 多视频库：character.ext, character2.ext, character3.ext …
            stems = {f.stem.lower() for f in assets.glob("character*")
                     if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv")}
            if "character" not in stems:
                dst = assets / f"character{ext}"
            else:
                n = 2
                while f"character{n}" in stems:
                    n += 1
                dst = assets / f"character{n}{ext}"
            tmp.replace(dst)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
        self.cfg.data["render"]["character"]["video"] = dst.name
        try:
            self._rebuild_renderers()
        except Exception as e:
            self.bus.log(f"人物视频已保存，但即时重载失败（下次重启生效）：{e}")
        self.bus.log(f"人物视频已启用: assets/{dst.name}"
                     f"（{len(raw) // 1024}KB，时长 {dur:.1f}s）")
        return {"ok": True, "path": f"assets/{dst.name}", "video": True,
                "duration": dur}

    _VIDEO_EXT = (".webm", ".mov", ".mp4", ".mkv")

    def _assets_dir(self) -> Path:
        """素材目录与配置文件同级（生产即项目根 assets/；测试实例自动隔离）。"""
        return Path(self.cfg_path).parent / "assets"

    def _videos_list(self) -> list[str]:
        """人物视频库：assets/character*.{webm,mov,mp4,mkv}。"""
        assets = self._assets_dir()
        out = [f.name for f in sorted(assets.glob("character*"))
               if f.suffix.lower() in self._VIDEO_EXT]
        cur = str((self.cfg.data["render"].get("character") or {}).get("video") or "")
        if cur and cur not in out:
            out.insert(0, cur)
        return out

    def video_delete(self, p: dict) -> dict:
        """删除视频库里的一个人物视频；删的是当前在用则自动切到下一个。"""
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9_.-]", "", str(p.get("name", "")))
        if not (name.startswith("character")
                and Path(name).suffix.lower() in self._VIDEO_EXT):
            return {"ok": False, "message": "只能删除 character* 人物视频"}
        assets = self._assets_dir()
        fp = assets / name
        if not fp.exists():
            return {"ok": False, "message": "文件不存在"}
        fp.unlink()
        chcfg = self.cfg.data["render"]["character"]
        if chcfg.get("video") == name:
            chcfg["video"] = None            # 先摘掉，避免 _videos_list 把死名字插回
            rest = self._videos_list()
            chcfg["video"] = rest[0] if rest else None
        self.save_config({})
        self._rebuild_renderers()
        self.bus.log(f"人物视频已删除: assets/{name}")
        return {"ok": True, "videos": self._videos_list(),
                "video": (self.cfg.data["render"]["character"] or {}).get("video")}

    def _character_video_name(self) -> str:
        assets = self._assets_dir()
        for ext in (".webm", ".mov", ".mp4", ".mkv"):
            for f in sorted(assets.glob(f"character*{ext}")):
                return f.name
        return ""

    # -- 白饭账本 -------------------------------------------------------------
    def ledger_adjust(self, p: dict) -> dict:
        """手动记账：delta>0 进账，delta<0 消耗。"""
        snap = self.app.ledger.adjust(_to_float(p.get("delta")))
        self.app.danmaku.set_balance(snap["balance"], snap.get("currency"))
        return {"ok": True, **snap}

    def ledger_reset(self, p: dict) -> dict:
        bal = p.get("balance")
        snap = self.app.ledger.reset(
            None if bal in (None, "") else _to_float(bal))
        self.app.danmaku.set_balance(snap["balance"], snap.get("currency"))
        return {"ok": True, **snap}

    def ledger_rules(self, p: dict) -> dict:
        """保存账本规则并即时生效（礼物价目/倍率/Token 折算）。"""
        patch: dict = {}
        gr = p.get("gift_rates")
        if isinstance(gr, dict):
            patch["gift_rates"] = {
                str(k).strip(): _to_float(v)
                for k, v in gr.items() if str(k).strip()}
        for k in ("start_balance", "income_multiplier", "spend_multiplier",
                  "default_coin_rate", "token_cost_per_million"):
            if k in p and p[k] not in (None, ""):
                patch[k] = _to_float(p[k])
        if p.get("currency"):
            patch["currency"] = str(p["currency"])[:8]
        self.save_config({"ledger": patch})
        snap = self.app.ledger.snapshot()
        self.app.danmaku.set_balance(snap["balance"], snap.get("currency"))
        return {"ok": True, **snap}

    # -- TTS（本地 GPT-SoVITS） ---------------------------------------------
    _TTS_EDITABLE = ("enabled", "provider", "base_url", "ref_audio_path",
                     "prompt_text", "prompt_lang", "text_lang", "speed_factor",
                     "seed", "timeout", "local_playback", "speak_types",
                     "templates", "volume")

    def update_tts(self, p: dict) -> dict:
        merged = _deep_merge(self.cfg.data.get("tts", {}) or {},
                             {k: v for k, v in p.items()
                              if k in self._TTS_EDITABLE})
        self.cfg.data["tts"] = merged       # TtsService 每次读配置，即时生效
        self.save_config({})                # 点「应用」即落盘到 config.json
        self.bus.log("TTS 设置已更新并保存（即时生效）")
        return {"ok": True}

    def tts_test(self, p: dict) -> dict:
        text = str(p.get("text") or "好的，测试一下，这是主播的声音～")
        r = self.app.tts.test(text)         # 冷启动可能要 20-30 秒，别着急
        msg = r.get("message") if not r.get("ok") \
            else f"{r['ms']}ms / {r['bytes']}B"
        self.bus.log(f"TTS 试听: {msg}")
        return r

    # -- 点歌中心 --------------------------------------------------------------
    _JUKE_EDITABLE = ("enabled", "bin", "trigger", "cooldown", "blacklist",
                      "queue_max", "play_seconds", "fade_seconds", "voice",
                      "music_volume", "bgm_enabled", "bgm_pool")

    def update_jukebox(self, p: dict) -> dict:
        merged = _deep_merge(self.cfg.data.get("jukebox", {}) or {},
                             {k: v for k, v in p.items()
                              if k in self._JUKE_EDITABLE})
        self.cfg.data["jukebox"] = merged
        self.save_config({})
        self.app.jukebox.ensure_worker()   # 开启/改配置后即时拉起（含 BGM）
        r = {"ok": True}
        appid = str(p.get("appid") or "").strip()
        pkey = str(p.get("privatekey") or "").strip()
        if appid or pkey:
            r = self.app.jukebox.configure(appid, pkey)
        self.bus.log(f"点歌设置已保存（{'已配置密钥' if appid or pkey else '无密钥变更'}）")
        return r

    def jukebox_test(self, p: dict) -> dict:
        r = self.app.jukebox.test(str(p.get("keyword") or "晴天"))
        self.bus.log(f"点歌测试: {'找到 ' + str(r['count']) + ' 首' if r['ok'] else '失败（检查密钥/登录）'}")
        return r

    def jukebox_request(self, p: dict) -> dict:
        r = self.app.jukebox.request(str(p.get("nickname") or "主播"),
                                     str(p.get("keyword") or ""))
        self.bus.log(f"手动点歌: {r.get('message') or r.get('song', {}).get('name')}")
        return r

    def jukebox_skip(self, p: dict) -> dict:
        r = self.app.jukebox.skip_current()
        self.bus.log("点歌: " + r["message"])
        return r

    def jukebox_clear(self, p: dict) -> dict:
        r = self.app.jukebox.clear_queue()
        self.bus.log("点歌: " + r["message"])
        return r

    def jukebox_login(self, p: dict) -> dict:
        r = self.app.jukebox.login()
        self.bus.log("点歌登录链接已生成（5 分钟内点开扫码）")
        return r

    def sfx_test(self, p: dict) -> dict:
        played = self.app.sfx.play()
        return {"ok": True, "played": played,
                "files": [f.name for f in self.app.sfx.files()],
                "message": "" if played else "没有可用的 wav 音效（先上传）"}

    # -- 配置保存 / 读取 -------------------------------------------------------
    def masked_ai_cfg(self) -> dict:
        ai = dict(self.cfg["ai"])
        key = ai.get("api_key", "")
        ai["api_key"] = "" if not key else "********（已设置，留空则不修改）"
        return ai

    def save_config(self, p: dict) -> dict:
        """把可编辑配置写回 config.json。凭据默认只存内存，勾选 remember 才落盘。"""
        merged = dict(self.cfg.data)
        if isinstance(p.get("ai"), dict):
            ai_patch = {k: v for k, v in p["ai"].items()
                        if k in self._AI_EDITABLE or k == "api_key"}
            if not str(ai_patch.get("api_key", "")).strip():
                ai_patch.pop("api_key", None)
            merged["ai"] = _deep_merge(merged.get("ai", {}), ai_patch)
        wss_patch = {}
        for k in ("room_id", "user_id", "device_id", "room_url"):
            if k in p.get("wss", {}):
                wss_patch[k] = str(p["wss"][k])
        if p.get("wss", {}).get("remember"):
            for k in ("cookie", "ttwid", "signature"):
                if k in p["wss"]:
                    wss_patch[k] = str(p["wss"][k])
        if "autostart" in p.get("wss", {}):   # 链接模式无需凭据也可 autostart
            wss_patch["autostart"] = bool(p["wss"].get("autostart"))
        merged["wss"] = _deep_merge(merged.get("wss", {}), wss_patch)
        rd = p.get("render") or {}
        if isinstance(rd.get("danmaku"), dict):
            merged["render"]["danmaku"] = _deep_merge(
                merged["render"].get("danmaku", {}), rd["danmaku"])
        if isinstance(rd.get("character"), dict):
            merged["render"]["character"] = _deep_merge(
                merged["render"].get("character", {}), rd["character"])
        if isinstance(rd.get("balance"), dict):
            merged["render"]["balance"] = _deep_merge(
                merged["render"].get("balance", {}), rd["balance"])
        lg = p.get("ledger") or {}
        if isinstance(lg, dict) and lg:
            merged["ledger"] = _deep_merge(merged.get("ledger", {}), lg)
        if isinstance(p.get("canvas"), dict):
            merged["canvas"] = _deep_merge(merged.get("canvas", {}), p["canvas"])
        if isinstance(p.get("jukebox"), dict):
            merged["jukebox"] = _deep_merge(merged.get("jukebox", {}), p["jukebox"])
        path = Path(self.cfg_path or "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        self.cfg.data = merged
        self.bus.log(f"配置已保存到 {path.name}")
        return {"ok": True, "path": str(path)}

    # -- 状态快照 --------------------------------------------------------------
    def state(self) -> dict:
        pipe = self.app.pipe
        return {
            "mode": ("mock画布 + Web控制台" if self.app.use_mock
                     else "native伴侣管道 + Web控制台"),
            "fps": self.cfg["fps"],
            "frames_sent": getattr(pipe, "frames_sent", 0),
            "app_connected": pipe.is_connected(),
            "wss": self.wss.info(),
            "events": dict(self.app.events.stats),
            "sim": {"running": self.sim is not None,
                    "interval": self._sim_interval},
            "ai": self.masked_ai_cfg(),
            "ledger": self.app.ledger.snapshot(),
            "canvas": self.cfg.get("canvas") or {},
            "tts": dict(self.cfg.get("tts") or {}),
            "tts_stats": dict(getattr(self.app, "tts", None)
                              and self.app.tts.stats or {}),
            "jukebox": {"cfg": dict(self.cfg.get("jukebox") or {}),
                        "status": self.app.jukebox.state()},
            "sfx": {**(self.cfg.get("sfx") or {}),
                    "available": sorted(f.name for f in
                                        (self._assets_dir()).glob("*.wav"))},
            "render": {"danmaku": self.cfg["render"]["danmaku"],
                       "character": dict(
                           self.cfg["render"]["character"],
                           video=(self.cfg["render"]["character"].get("video")
                                  or (self._videos_list() or [""])[0]),
                           videos=self._videos_list()),
                       "balance": self.cfg["render"].get("balance", {}),
                       "songq": self.cfg["render"].get("songq", {}),
                       "bubble": self.cfg["render"].get("bubble", {}),
                       "histo": self.cfg["render"].get("histo", {}),
                       "spectrum": {"available": bool(getattr(self.app, "spectrum", None)
                                                      and self.app.spectrum.available),
                                    "error": (getattr(self.app, "spectrum", None)
                                              and self.app.spectrum.last_error) or ""},
                       "font_path": self.cfg["render"].get("font_path", "")},
            "gift_names": sorted({g.get("gift_name", "")
                                  for g in self.app.gifts._by_id.values()}
                                 - {""}),
            "config_wss": {k: v for k, v in (self.cfg.get("wss") or {}).items()
                           if k in ("room_id", "user_id", "device_id",
                                    "room_url", "autostart")},
            "has_credentials_saved": bool(
                (self.cfg.get("wss") or {}).get("cookie")),
        }


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    console: WebConsole = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # 静音默认 stderr 访问日志
        pass

    # -- 基础应答 -------------------------------------------------------------
    def _json(self, obj, status=200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _file(self, path: Path, ctype: str) -> None:
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # -- GET ------------------------------------------------------------------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        c = self.console
        if u.path == "/":
            self._file(_STATIC / "index.html", "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._json(c.state())
        elif u.path == "/api/ledger":
            self._json(c.app.ledger.snapshot())
        elif u.path == "/api/history":
            self._json({"events": c.bus.history(), "logs": c.bus.logs()})
        elif u.path == "/api/stream":
            self._sse()
        elif u.path == "/mjpeg":
            self._mjpeg()
        elif u.path == "/preview.jpg":
            seq, jpg = c.preview.snapshot()
            if not jpg:
                self._json({"message": "还没有帧"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpg)
        else:
            self._json({"message": "not found"}, 404)

    # -- POST -----------------------------------------------------------------
    def do_POST(self) -> None:
        u = urlparse(self.path)
        c = self.console
        body = self._body()
        try:
            if u.path == "/api/wss/connect":
                self._json(c.wss.connect(body))
            elif u.path == "/api/wss/disconnect":
                self._json(c.wss.disconnect())
            elif u.path == "/api/sim/start":
                self._json(c.sim_start(body.get("interval", 1.8)))
            elif u.path == "/api/sim/stop":
                self._json(c.sim_stop())
            elif u.path == "/api/inject":
                self._json(c.inject_event(body))
            elif u.path == "/api/ai":
                self._json(c.update_ai(body))
            elif u.path == "/api/render":
                self._json(c.apply_render(body))
            elif u.path == "/api/upload":
                self._json(c.upload(body))
            elif u.path == "/api/video_delete":
                self._json(c.video_delete(body))
            elif u.path == "/api/ledger/adjust":
                self._json(c.ledger_adjust(body))
            elif u.path == "/api/ledger/reset":
                self._json(c.ledger_reset(body))
            elif u.path == "/api/ledger/rules":
                self._json(c.ledger_rules(body))
            elif u.path == "/api/tts":
                self._json(c.update_tts(body))
            elif u.path == "/api/tts/test":
                self._json(c.tts_test(body))
            elif u.path == "/api/jukebox":
                self._json(c.update_jukebox(body))
            elif u.path == "/api/jukebox/test":
                self._json(c.jukebox_test(body))
            elif u.path == "/api/jukebox/req":
                self._json(c.jukebox_request(body))
            elif u.path == "/api/jukebox/skip":
                self._json(c.jukebox_skip(body))
            elif u.path == "/api/jukebox/clear":
                self._json(c.jukebox_clear(body))
            elif u.path == "/api/jukebox/login":
                self._json(c.jukebox_login(body))
            elif u.path == "/api/sfx/test":
                self._json(c.sfx_test(body))
            elif u.path == "/api/config/save":
                self._json(c.save_config(body))
            elif u.path == "/api/quit":
                self._json({"ok": True, "message": "控制台退出中"})
                c._quit.set()
                threading.Timer(0.3, c.stop).start()
                threading.Timer(0.6, self.server.shutdown).start()  # type: ignore[attr-defined]
            else:
                self._json({"message": "not found"}, 404)
        except Exception as e:
            self.console.bus.log(f"API 错误 {u.path}: {e!r}", "error")
            self._json({"ok": False, "message": repr(e),
                        "trace": traceback.format_exc(limit=3)}, 500)

    # -- 流式端点 --------------------------------------------------------------
    def _sse(self) -> None:
        c = self.console
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q = c.bus.subscribe()
        try:
            for item in c.bus.history():
                self.wfile.write(sse_encode(item))
            self.wfile.flush()
            while not c._quit.is_set():
                try:
                    item = q.get(timeout=15)
                except Exception:
                    self.wfile.write(b": ping\n\n")   # 保活
                    self.wfile.flush()
                    continue
                self.wfile.write(sse_encode(item))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            c.bus.unsubscribe(q)

    def _mjpeg(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Connection", "close")
        self.end_headers()
        c = self.console
        last_seq = -1
        try:
            while not c._quit.is_set():
                seq, jpg = c.preview.snapshot()
                if seq != last_seq and jpg:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    self.wfile.flush()
                    last_seq = seq
                time.sleep(1.0 / 12)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ---------------------------------------------------------------------------
class QuietServer(ThreadingHTTPServer):
    """浏览器/SSE 客户端中途断线是常态，别打整屏堆栈。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def serve(console: WebConsole, host: str = "127.0.0.1", port: int = 8765) -> None:
    Handler.console = console
    srv = QuietServer((host, port), Handler)
    console._srv = srv
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="http-server").start()
