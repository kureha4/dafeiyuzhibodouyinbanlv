"""主程序：组装全部模块，作为互动插件 exe 的入口。

两种运行方式：
1) 接真实直播伴侣（native 模式）
   在开放平台创建互动插件(exe 型)后，把产物包提审；调试阶段用官方调试伴侣包，
   互动插件调试面板 AppID 填 MAIN/随机，启动文件选本程序（或它包装出的 exe）。
   伴侣会以
       main.exe --pipeName=784612345 --maxChannels=6 --mateVersion=8.0.0 --layoutMode=0
   拉起进程，程序自动连接命名管道开始工作。
   python -m liveplugin.main --pipeName=... --maxChannels=6 ...

2) 本地自测（mock 模式，默认）
   python -m liveplugin.main --mock --sim
   --sim 附带随机观众事件模拟器 + 本机窗口预览两路上屏画面（等效于伴侣画布）。
"""
from __future__ import annotations

import argparse
import os
import random
import threading
import time

from .ai import AIOrchestrator
from .audiospectrum import SpectrumSource
from .config import Config
from .events import EventCenter, LiveEvent
from .gifts import GiftCatalog
from .ipc import MockPipeClient, NativePipeClient, Packet
from .jukebox import Jukebox
from .ledger import RiceLedger
from .render import (BalanceRenderer, BubbleRenderer, CharacterRenderer,
                     DanmakuRenderer, HistoRenderer, SongQueueRenderer)
from .sfx import GiftSfx
from .tts import TtsService, wav_to_pcm

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 进场/关注/点赞的 TTS 模板（config.json tts.templates 可覆盖）
_TTS_TPL = {"member": "欢迎 {nickname} 进入直播间～",
            "follow": "感谢 {nickname} 的关注～",
            "like": "感谢 {nickname} 的点赞支持～"}

NICKS = ["快乐小狗", "阿泽", "momo", "追风少年", "柚子茶", "深夜食客",
         "小仙女", "数据猿阿强", "干饭第一名", "喵呜"]
COMMENTS = ["主播好漂亮", "666", "这首歌叫什么", "今天唱什么歌",
            "来了来了", "主播唱一首起风了", "支持主播", "这个弹幕墙好好看",
            "AI 主播？", "哈哈哈哈笑死", "主播几岁", "粉了粉了"]
GIFTS = GiftCatalog()


class Simulator(threading.Thread):
    """模拟互动消息生成器（等效调试面板"模拟互动消息"按钮）。"""

    def __init__(self, on_payload, interval: float = 1.8) -> None:
        super().__init__(daemon=True, name="sim")
        self.on_payload = on_payload
        self.interval = interval
        self._stop = threading.Event()
        self._seq = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        g = random.choice(list(GIFTS._by_id.items()))
        while not self._stop.wait(self.interval * random.uniform(0.5, 1.6)):
            self._seq += 1
            ts = int(time.time() * 1000)
            nick = random.choice(NICKS)
            uid = f"u_{hash(nick) % 1000:04d}"
            roll = random.random()
            base = {"msg_id": f"sim-{self._seq}", "timestamp": ts,
                    "sec_open_id": uid, "nickname": nick,
                    "avatar_url": "", "user_privilege_level": random.randint(1, 3),
                    "is_follow_anchor": random.random() < 0.4,
                    "fansclub_level": random.randint(0, 2)}
            if roll < 0.5:
                base.update(msg_type=2, msg_type_str="live_comment",
                            content=random.choice(COMMENTS))
            elif roll < 0.75:
                sid, item = random.choice(list(GIFTS._by_id.items()))
                base.update(msg_type=3, msg_type_str="live_gift",
                            sec_gift_id=sid, gift_num=random.choice([1, 1, 1, 2, 5]))
            elif roll < 0.95:
                base.update(msg_type=1, msg_type_str="live_like",
                            like_num=random.randint(1, 20))
            else:
                base.update(msg_type=5, msg_type_str="live_follow",
                            user_follow_action=1)
            self.on_payload([base])


class PreviewWindow:
    """mock 模式的本机画布预览：把两路素材帧合成显示（等效伴侣画布）。"""

    def __init__(self, layout: dict) -> None:
        import tkinter as tk
        from PIL import Image, ImageTk
        self._ImageTk, self._Image = ImageTk, Image
        self.layout = layout
        self.root = tk.Tk()
        self.root.title("直播伴侣画布预览（模拟）")
        cw, ch = 540, 960
        self.canvas = tk.Canvas(self.root, width=cw, height=ch, bg="#101018")
        self.canvas.pack()
        self._base = Image.new("RGBA", (cw, ch), (24, 28, 44, 255))
        self._bg = Image.new("RGB", (cw, ch))
        self._bg.paste(self._base, (0, 0))
        self._photo = self._ImageTk.PhotoImage(self._bg)
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._pending: dict[int, Packet] = {}

    def compose(self, channel_id: int, pkt: Packet) -> None:
        """仅缓存最新帧，绘制统一放到 UI 线程的 tick()。"""
        if not self._closed.is_set():
            self._pending[channel_id] = pkt

    def tick(self) -> None:
        if self._closed.is_set() or not self._pending:
            self.root.update_idletasks()
            self.root.update()
            return
        frames = dict(self._pending)
        self._pending.clear()
        try:
            out = self._base.copy()
            for ch_id, pkt in frames.items():
                lay = self.layout.get(ch_id)
                if not lay:
                    continue
                img = self._Image.frombytes(
                    "RGBA", (pkt.video.cx, pkt.video.cy), pkt.data).resize(
                    (lay["w"], lay["h"]), self._Image.LANCZOS)
                out.alpha_composite(img, (lay["x"], lay["y"]))
            self._bg.paste(out.convert("RGB"), (0, 0))
            self._photo = self._ImageTk.PhotoImage(self._bg)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        except Exception:
            pass
        self.root.update_idletasks()
        self.root.update()

    def close(self) -> None:
        self._closed.set()
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
class LivePluginApp:
    def __init__(self, cfg: Config, pipe_name: str | None = None,
                 max_channels: int = 6, mate_version: str = "0",
                 layout_mode: int = 0, use_mock: bool = False,
                 with_preview: bool = False, with_sim: bool = False,
                 wss: dict | None = None) -> None:
        self.cfg = cfg
        self.mate_version = mate_version
        mode = cfg["mode"] if not use_mock else "mock"
        if pipe_name:                       # 伴侣拉起时必带 pipeName，强制 native
            mode = "native"
        self.use_mock = mode == "mock"
        self.pipe_name = pipe_name or ("mock" if self.use_mock else None)
        if not self.use_mock and not pipe_name:
            raise ValueError("native 模式必须提供 --pipeName（由伴侣注入）")

        self.pipe = MockPipeClient() if self.use_mock else NativePipeClient()

        self._preview: PreviewWindow | None = None
        if with_preview and self.use_mock:
            self._preview = PreviewWindow(_preview_layout(cfg, layout_mode))
            self.pipe.set_frame_sink(self._on_frame_local)

        # 业务模块
        self.gifts = GiftCatalog()
        self.events = EventCenter(self.gifts)
        ch = cfg["channels"]
        self.character = CharacterRenderer(
            dict(cfg["render"], fps=cfg["fps"]), ch["character"]["width"],
            ch["character"]["height"],
            asset_dir=_asset_dir())
        self.danmaku = DanmakuRenderer(
            dict(cfg["render"]["danmaku"], font_path=cfg["render"]["font_path"]),
            ch["danmaku"]["width"], ch["danmaku"]["height"],
            channel_id=ch["danmaku"]["id"])
        # 白饭账本（状态存 ledger.json，规则读 config.json 的 ledger 段）
        # LIVEPLUGIN_LEDGER 可指定账本文件（测试/多实例用）
        self.ledger = RiceLedger(
            os.environ.get("LIVEPLUGIN_LEDGER") or (_ROOT / "ledger.json"),
            lambda: self.cfg.get("ledger") or {})
        # 本地 GPT-SoVITS TTS（队列+后台线程，不阻塞直播循环）
        self.tts = TtsService(lambda: self.cfg.get("tts") or {},
                              on_wav=self._on_tts_wav)
        # 点歌中心（弹幕「点歌 歌名」→ 官方 ncm-cli 搜索+本机播放）
        self.jukebox = Jukebox(lambda: self.cfg.get("jukebox") or {},
                               log=lambda m: print(f"[点歌] {m}"),
                               on_speak=self._on_song_speak)
        # 礼物音效（外部上传的 wav，收到礼物随机播一个；没有文件就安静）
        self.sfx = GiftSfx(lambda: self.cfg.get("sfx") or {},
                           _asset_dir() or (_ROOT / "assets"),
                           log=lambda m: print(f"[音效] {m}"))
        # 独立「余额面板」通道：每帧直接读账本，无需手动同步
        bc = ch.get("balance") or {}
        self.balance = BalanceRenderer(
            dict(cfg["render"].get("balance") or {},
                 font_path=cfg["render"]["font_path"]),
            int(bc.get("width", 520)), int(bc.get("height", 170)),
            channel_id=int(bc.get("id", 3)))
        self.balance.set_source(lambda: self.ledger.snapshot())
        # 「点歌队列」面板：每帧热读 jukebox 队列状态
        sc = ch.get("songq") or {}
        self.songq = SongQueueRenderer(
            dict(cfg["render"].get("songq") or {},
                 font_path=cfg["render"]["font_path"]),
            int(sc.get("width", 520)), int(sc.get("height", 300)),
            channel_id=int(sc.get("id", 4)))
        self.songq.set_source(lambda: self.jukebox.panel())
        # 「AI 气泡」通道：大模型回复 → 画布上的对话气泡（自动过期）
        pc = ch.get("bubble") or {}
        self.bubble = BubbleRenderer(
            dict(cfg["render"].get("bubble") or {},
                 font_path=cfg["render"]["font_path"]),
            int(pc.get("width", 620)), int(pc.get("height", 190)),
            channel_id=int(pc.get("id", 5)))
        # 「音乐频谱」通道：环回采集本机出声 → 随音乐跳动的直方图
        hc = ch.get("histo") or {}
        hcfg = dict(cfg["render"].get("histo") or {})
        self.histo = HistoRenderer(hcfg, int(hc.get("width", 460)),
                                   int(hc.get("height", 140)),
                                   channel_id=int(hc.get("id", 6)))
        self.spectrum = None
        if hcfg.get("enabled", True):
            self.spectrum = SpectrumSource(
                bars=int(hcfg.get("bars", 28) or 28),
                sensitivity=float(hcfg.get("sensitivity", 1.0) or 1.0))
            self.histo.set_source(self.spectrum.levels)
        self.ai = AIOrchestrator(
            dict(cfg["ai"], char_name=cfg["render"]["character"]["name"]),
            self._on_reply, character=self.character,
            on_usage=self._on_usage)
        self._sync_badge()
        self.events.on_event(self._on_live_event)

        self._stop = threading.Event()
        self.max_channels = max_channels

        # WSS 逆向数据源（替代官方 OPEN_LIVE_DATA，含官方没有的进场消息）
        self.wss_source = None
        if wss and wss.get("room_id"):
            from .wss_source import DouyinWssSource
            self.wss_source = DouyinWssSource(
                lambda payload: self.events.handle_partner_event(
                    "OPEN_LIVE_DATA", {"payload": payload}),
                verbose=bool(wss.get("verbose", True)))
            self._wss_cfg = wss

    # -- 生命周期 -------------------------------------------------------------
    def run(self, duration: float = 0) -> None:
        print(f"[app] 启动，mode={'mock' if self.use_mock else 'native'} "
              f"pipe={self.pipe_name} maxChannels={self.max_channels}")
        if not self.pipe.connect(self.pipe_name, self.max_channels):
            print("[app] 管道连接失败（检查伴侣是否已启动插件、pipeName 是否正确）")
            return
        self.pipe.set_event_handler(self.events.handle_partner_event)

        # 数据源选择：WSS 逆向优先（含进场消息），否则用官方管道 OPEN_LIVE_DATA
        if self.wss_source is not None:
            print(f"[app] 数据源=WSS逆向 room_id={self._wss_cfg.get('room_id')}")
            self._connect_wss()
        else:
            print("[app] 数据源=官方管道 OPEN_LIVE_DATA")
            # 订阅直播间开放数据（评论/礼物/点赞/粉丝团/关注 五类都订上）
            for ev_name in ("OPEN_LIVE_DATA",):
                r = self.pipe.subscribe_event(ev_name)
                print(f"[app] subscribe {ev_name} -> code={r.get('code')}")

        # 礼物池（伴侣>=8.0.0，仅官方模式有意义）
        pool = self.pipe.get_open_gift_pools()
        if pool.get("code") == 1:
            added = self.gifts.merge_pool(pool["data"].get("gift_pool", []))
            print(f"[app] 礼物池合并 +{added} 款（共 {len(self.gifts)}）")

        self.ai.start()
        self.jukebox.ensure_worker()      # 开播即拉起（没点歌也自动垫 BGM）
        fps = self.cfg["fps"]
        period = 1.0 / fps
        deadline = (time.time() + duration) if duration > 0 else None
        print("[app] 进入渲染循环" + (f"（{duration:g}s 后自动退出）" if deadline else "（Ctrl+C 退出）"))
        try:
            while not self._stop.is_set() and self.pipe.is_connected():
                t0 = time.time()
                self.pipe.write_packet(self.character.next_frame())
                self.pipe.write_packet(self.danmaku.next_frame())
                if self.balance.enabled:
                    self.pipe.write_packet(self.balance.next_frame())
                if self.songq.enabled:
                    self.pipe.write_packet(self.songq.next_frame())
                if self.bubble.enabled:
                    self.pipe.write_packet(self.bubble.next_frame())
                if self.histo.enabled:
                    self.pipe.write_packet(self.histo.next_frame())
                if self._preview:
                    self._preview.tick()
                if deadline and time.time() >= deadline:
                    break
                dt = time.time() - t0
                time.sleep(max(0.0, period - dt))
        except KeyboardInterrupt:
            pass
        finally:
            print("[app] 退出（伴侣侧事件 EVENT_DISCONNECTED 或用户中断）")
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        if self.spectrum is not None:
            self.spectrum.stop()
        self.ai.stop()
        if self.wss_source is not None:
            self.wss_source.close()
        self.pipe.remove_all_media_sources() if not self.use_mock else None
        self.pipe.shutdown()
        if self._preview:
            self._preview.close()

    def _connect_wss(self) -> None:
        """建立 WSS 逆向连接（鉴权参数从浏览器直播间会话抓取，见 README）。"""
        w = self._wss_cfg
        try:
            self.wss_source.connect(
                w["room_id"], user_id=str(w.get("user_id", "0")),
                device_id=w.get("device_id", ""),
                cookie=w.get("cookie", ""),
                ttwid=w.get("ttwid", ""),
                signature=w.get("signature", ""))
            print("[wss] 线程已启动（等待 on_open 回调，连接失败会打印 error）")
        except Exception as e:
            print(f"[wss] 连接启动失败: {e}")

    # -- 事件流转 -------------------------------------------------------------
    def _on_live_event(self, ev: LiveEvent) -> None:
        self.danmaku.push_event(ev)      # 1. 弹幕墙立刻上墙
        if ev.type == "gift":            # 2. 礼物进账（白饭账本）+ 金币音效
            self.ledger.on_gift(ev.gift_name, ev.gift_num, ev.gift_value)
            self._sync_badge()
            self.sfx.play()
        # 3. 进场/关注/点赞：不推 AI，按 tts.speak_types 直接模板语音（可自行改模板）
        if self.tts.want(ev.type) and ev.type in _TTS_TPL:
            tpl = ((self.cfg.get("tts") or {}).get("templates")
                   or {}).get(ev.type) or _TTS_TPL[ev.type]
            try:
                self.tts.speak(tpl.format(nickname=ev.nickname,
                                          like_num=ev.like_num))
            except Exception:
                pass
        # 4. 弹幕点歌命令：归点歌台管，命中就不再喂 AI，
        #    否则 AI 会以为观众让它唱歌、回一句「我不会唱」拆台
        if ev.type == "comment" and \
                self.jukebox.handle_comment(ev.nickname, ev.content):
            return
        self.ai.submit(ev)               # 5. 交给 AI 编排（白名单+限流在 AI 内部）

    def _on_song_speak(self, text: str) -> None:
        if self.tts.want("song"):
            self.tts.speak(text)

    def _on_tts_wav(self, wav: bytes) -> None:
        """native 模式：TTS 的 wav 转 48k 立体声 PCM 推伴侣音频通道。"""
        if self.use_mock:
            return
        try:
            aid = (self.cfg["channels"].get("audio") or {}).get("id", 2)
            self.pipe.write_packet(Packet.make_audio(aid, wav_to_pcm(wav)))
        except Exception as e:
            print(f"[tts] 音频通道推送失败: {e}")

    def _sync_badge(self) -> None:
        """刷新弹幕墙标题栏的余额角标。"""
        s = self.ledger.snapshot()
        self.danmaku.set_balance(s["balance"], s.get("currency"))

    def _on_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """AI 调用返回的 token 用量 → 白饭消耗。"""
        self.ledger.on_tokens(prompt_tokens, completion_tokens)
        self._sync_badge()

    def _on_reply(self, text: str, ev: LiveEvent) -> None:
        # 回复：画布气泡 + 上弹幕墙（金色气泡）+ 朗读（AI 回复语音，可在控制台开关）
        self.bubble.show(text)
        self.danmaku.push_reply(ev.nickname, text)
        print(f"[reply->{ev.type}] {ev.nickname}: {text}")
        if self.tts.want("reply"):
            self.tts.speak(text)

    def _on_frame_local(self, pkt: Packet) -> None:
        if self._preview:
            self._preview.compose(pkt.channel_id, pkt)


def _asset_dir():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "assets"
    return p if p.exists() else None


def _preview_layout(cfg: Config, layout_mode: int) -> dict:
    ch = cfg["channels"]
    bc = ch.get("balance") or {}
    sc = ch.get("songq") or {}
    pc = ch.get("bubble") or {}
    hc = ch.get("histo") or {}
    return {
        ch["character"]["id"]: {"x": 40, "y": 0, "w": 300, "h": 450},
        ch["danmaku"]["id"]: {"x": 350, "y": 30, "w": 175, "h": 780},
        bc.get("id", 3): {"x": 40, "y": 460, "w": 300, "h": 98},
        sc.get("id", 4): {"x": 40, "y": 566, "w": 300, "h": 173},
        pc.get("id", 5): {"x": 40, "y": 748, "w": 310, "h": 95},
        hc.get("id", 6): {"x": 40, "y": 851, "w": 310, "h": 70},
    }


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="抖音直播伴侣 AI 虚拟主播互动插件")
    ap.add_argument("--pipeName", default=None)
    ap.add_argument("--maxChannels", type=int, default=6)
    ap.add_argument("--mateVersion", default="0")
    ap.add_argument("--layoutMode", type=int, default=0, choices=[0, 1])
    ap.add_argument("--config", default=None, help="config.json 路径")
    ap.add_argument("--mock", action="store_true", help="强制本地内存模式")
    ap.add_argument("--preview", action="store_true", help="本机窗口预览画布")
    ap.add_argument("--sim", action="store_true", help="启动模拟互动消息")
    ap.add_argument("--duration", type=float, default=0,
                    help="运行多少秒后自动退出（0=一直运行），用于冒烟测试")
    ap.add_argument("--wss-room-id", default=None,
                    help="启用 WSS 逆向数据源并指定直播间 room_id_str")
    return ap.parse_known_args(argv)[0]     # 容忍伴侣传入的未知参数


def _build_wss_cfg(args, cfg: Config) -> dict | None:
    """组装 WSS 逆向参数：room_id 走命令行/配置文件，鉴权参数一律走环境变量
    （cookie/ttwid/signature 属敏感凭据，不落盘、不进版本库）。"""
    room_id = args.wss_room_id or (cfg.get("wss") or {}).get("room_id") or ""
    if not room_id:
        return None
    import os
    return {
        "room_id": str(room_id),
        "user_id": os.environ.get("LIVEPLUGIN_USER_ID", "0"),
        "device_id": os.environ.get("LIVEPLUGIN_DEVICE_ID", ""),
        "cookie": os.environ.get("LIVEPLUGIN_COOKIE", ""),
        "ttwid": os.environ.get("LIVEPLUGIN_TTWID", ""),
        "signature": os.environ.get("LIVEPLUGIN_SIGNATURE", ""),
        "verbose": True,
    }


def main(argv=None) -> None:
    args = parse_args(argv)
    cfg = Config.load(args.config)
    wss = _build_wss_cfg(args, cfg)
    app = LivePluginApp(cfg, pipe_name=args.pipeName,
                        max_channels=args.maxChannels,
                        mate_version=args.mateVersion,
                        layout_mode=args.layoutMode,
                        use_mock=args.mock or cfg["mode"] == "mock",
                        with_preview=args.preview or args.sim,
                        with_sim=args.sim,
                        wss=wss)
    if args.sim and isinstance(app.pipe, MockPipeClient):
        sim = Simulator(lambda payload: app.pipe.inject_event(
            "OPEN_LIVE_DATA", {"payload": payload}))
        sim.start()
        print("[app] 模拟互动消息已开启")
    app.run(duration=args.duration)


if __name__ == "__main__":
    main()
