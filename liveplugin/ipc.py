r"""IPC 协议层：与直播伴侣之间通过命名管道传输「音视频帧 + JSON 指令/事件」。

协议要点（对齐官方《互动工具开发者指南（仅直播伴侣）》2024.09 版）：
- 伴侣以 ``plugin.exe --pipeName=xxx --maxChannels=6 --mateVersion=7.7.0 --layoutMode=0``
  启动插件进程；pipeName 即 Windows 命名管道名（\\.\pipe\{pipeName}）。
- 音视频：每个素材通道对应画布上一个素材；首次建连默认存在素材通道 0，可直接推帧；
  更多通道调用 createMediaSources 申请。帧结构对应官方 IPC_PACKET（Video/Audio + data）。
- 视频默认像素格式 RGBA（带透明通道），单通道分辨率 ≤ 1920x1080。
- 指令：type=request 的 JSON（请求-响应，reqId 配对）；事件：type=event 的 JSON
  （订阅-发布，先 x.subscribeEvent 才能收到）。
- 收到 EVENT_DISCONNECTED 事件必须退出进程。

本文件把「传输」抽象成 PipeClient：
- MockPipeClient：纯内存实现，供本地端到端自测（伴侣侧行为做了轻量仿真：
  响应 createMediaSources / x.subscribeEvent，定时回放模拟事件）。
- NativePipeClient：真实伴侣模式。官方 SDK 是 C++ 的 PipeSDK.dll；纯 Python 侧
  可以按同一帧协议直接读写命名管道（write_frame/send_json 已给出实现框架），
  若公司要求严格走 SDK，可在 C++ 宿主里链 PipeSDK.dll，再以本机 socket/共享内存
  把帧转给本 Python 进程——两种接入点都预留了，默认走直接管道读写。
"""
from __future__ import annotations

import abc
import json
import queue
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# 官方 IPC 枚举（子集，与 SDK 头文件命名对齐）
# ---------------------------------------------------------------------------

class IPC_VIDEO_PIXEL_FORMAT:
    NONE = 0
    I420 = 1
    RGBA = 2        # 默认使用 RGBA 带透明通道（官方文档原文）
    BGRA = 3
    NV12 = 4

class IPC_EVENT_TYPE:
    """SDK 回调里的事件类型（官方命名）。"""
    EVENT_CONNECTED = 0
    EVENT_DISCONNECTED = 1
    EVENT_MESSAGE = 2


@dataclass
class VideoSpec:
    """对应官方 struct Video。"""
    cx: int
    cy: int
    frame_rate: tuple = (1, 30)                 # IPC_RATIONAL，默认 1/30
    format: int = IPC_VIDEO_PIXEL_FORMAT.RGBA
    angle: int = 0
    bFlipH: bool = False
    bFlipV: bool = False
    cs: int = 0
    ct: int = 0
    range_: int = 0


@dataclass
class AudioSpec:
    """对应官方 struct Audio。"""
    format: int = 1          # PCM
    layout: int = 3          # 立体声
    sample_rate: int = 48000
    count: int = 480         # 每包采样数（20ms@48k）
    planes: int = 1
    blocksize: int = 4       # 16bit * 2ch


@dataclass
class Packet:
    """对应官方 IPC_PACKET：帧数据 + 通道 + 时间戳。"""
    channel_id: int
    data: bytes
    timestamp: int           # ns，用于音画同步（官方 GetTimeNS）
    video: VideoSpec | None = None
    audio: AudioSpec | None = None

    @staticmethod
    def make_video(channel_id: int, rgba: bytes, cx: int, cy: int,
                   fps: int = 30) -> "Packet":
        return Packet(channel_id=channel_id, data=rgba,
                      timestamp=time.time_ns(),
                      video=VideoSpec(cx=cx, cy=cy, frame_rate=(1, fps)))

    @staticmethod
    def make_audio(channel_id: int, pcm: bytes, spec: AudioSpec | None = None) -> "Packet":
        return Packet(channel_id=channel_id, data=pcm,
                      timestamp=time.time_ns(), audio=spec or AudioSpec())


# ---------------------------------------------------------------------------
# 帧的二进制封装（Mock 与 Native 共用同一布局，便于替换实现）
# native 端对接官方 WritePacket 时，把 Packet 展开为 IPC_PACKET 即可。
# ---------------------------------------------------------------------------
_FRAME_MAGIC = b"LPAV"


def pack_frame(pkt: Packet) -> bytes:
    """把 Packet 封装成自描述字节帧。"""
    if pkt.video is not None:
        kind = 1
        # 参数序: kind ch cx cy ts(ns,i64) rateNum rateDen fmt angle flipH flipV cs ct range
        meta = struct.pack("<BiiiqiiBi??BBB",
                           kind, pkt.channel_id,
                           pkt.video.cx, pkt.video.cy, pkt.timestamp,
                           *pkt.video.frame_rate,
                           pkt.video.format, pkt.video.angle,
                           pkt.video.bFlipH, pkt.video.bFlipV,
                           pkt.video.cs, pkt.video.ct, pkt.video.range_)
    elif pkt.audio is not None:
        kind = 2
        meta = struct.pack("<BiiiIII", kind, pkt.channel_id, pkt.timestamp,
                           pkt.audio.format, pkt.audio.layout,
                           pkt.audio.sample_rate, len(pkt.data))
    else:
        raise ValueError("Packet must carry video or audio")
    return _FRAME_MAGIC + struct.pack("<I", len(meta)) + meta + struct.pack("<I", len(pkt.data)) + pkt.data


def unpack_frame(buf: bytes) -> Packet:
    if not buf.startswith(_FRAME_MAGIC):
        raise ValueError("bad frame magic")
    off = 4
    (meta_len,) = struct.unpack_from("<I", buf, off); off += 4
    (kind,) = struct.unpack_from("<B", buf, off)
    if kind == 1:
        (_k, ch, cx, cy, ts, nr, dn, fmt, angle, fh, fv, cs, ct, rng) = \
            struct.unpack_from("<BiiiqiiBi??BBB", buf, off)
        spec = VideoSpec(cx=cx, cy=cy, frame_rate=(nr, dn), format=fmt,
                         angle=angle, bFlipH=fh, bFlipV=fv, cs=cs, ct=ct, range_=rng)
        off += meta_len
        (dlen,) = struct.unpack_from("<I", buf, off); off += 4
        return Packet(channel_id=ch, data=buf[off:off+dlen], timestamp=ts, video=spec)
    elif kind == 2:
        (_k, ch, ts, fmt, layout, rate, dlen) = struct.unpack_from("<BiiiIII", buf, off)
        spec = AudioSpec(format=fmt, layout=layout, sample_rate=rate)
        off += meta_len
        (size,) = struct.unpack_from("<I", buf, off); off += 4
        return Packet(channel_id=ch, data=buf[off:off+size], timestamp=ts, audio=spec)
    raise ValueError(f"unknown frame kind {kind}")


# ---------------------------------------------------------------------------
# PipeClient 抽象
# ---------------------------------------------------------------------------
OnEvent = Callable[[str, dict], None]          # (eventName, params)
OnFrame = Callable[[Packet], None]             # Mock 的"伴侣侧"收帧钩子


class PipeClient(abc.ABC):
    """插件 ↔ 伴侣 的传输抽象。"""

    def __init__(self) -> None:
        self._disconnected = threading.Event()

    # -- 生命周期 -----------------------------------------------------------
    @abc.abstractmethod
    def connect(self, pipe_name: str, max_channels: int = 6) -> bool: ...

    @abc.abstractmethod
    def shutdown(self) -> None: ...

    def is_connected(self) -> bool:
        return not self._disconnected.is_set()

    def wait_disconnected(self, timeout: float | None = None) -> bool:
        return self._disconnected.wait(timeout)

    def _mark_disconnected(self) -> None:
        self._disconnected.set()

    # -- 音视频帧 -----------------------------------------------------------
    @abc.abstractmethod
    def write_packet(self, pkt: Packet, timeout_ms: int = 1000) -> bool: ...

    def write_packet_async(self, pkt: Packet) -> bool:
        """异步模式：最快上屏，不保证播放同步。默认直接转同步实现。"""
        return self.write_packet(pkt, timeout_ms=0)

    # -- JSON 指令（请求-响应 / 订阅-发布）------------------------------------
    @abc.abstractmethod
    def send_message(self, payload: dict) -> None:
        """发送 type=request 的 JSON。"""

    def request(self, method: str, params: dict | None = None,
                timeout: float = 5.0) -> dict:
        """请求-响应封装：自动生成 reqId 并等待配对响应。"""
        req_id = uuid.uuid4().hex
        evt = threading.Event()
        result: dict = {}

        def _pair(resp: dict) -> None:
            nonlocal result
            result = resp
            evt.set()

        self._pending[req_id] = _pair
        self.send_message({"type": "request", "reqId": req_id,
                           "method": method, "params": params or {}})
        if not evt.wait(timeout):
            self._pending.pop(req_id, None)
            return {"code": -1, "message": "request timeout", "data": {}}
        return result

    # -- 便捷 API（与官方 method 名一一对应）---------------------------------
    def create_media_sources(self, count: int) -> dict:
        return self.request("createMediaSources", {"count": count})

    def remove_media_sources(self, source_ids: list[int]) -> dict:
        return self.request("removeMediaSources", {"sourceIds": source_ids})

    def get_current_media_sources(self) -> dict:
        return self.request("getCurrentMediaSources")

    def remove_all_media_sources(self) -> dict:
        return self.request("removeAllMediaSources")

    def subscribe_event(self, event_name: str) -> dict:
        return self.request("x.subscribeEvent",
                            {"eventName": event_name,
                             "timestamp": int(time.time() * 1000)})

    def unsubscribe_event(self, event_name: str) -> dict:
        return self.request("x.unsubscribeEvent", {"eventName": event_name})

    def publish_event(self, event_name: str, params: dict) -> dict:
        return self.request("x.publishEvent",
                            {"eventName": event_name, "params": params})

    def get_open_gift_pools(self) -> dict:
        """伴侣 >= 8.0.0：获取礼物池（sec_gift_id→名字/图标/价格）。"""
        return self.request("getOpenGiftPools")

    def get_open_room_data(self) -> dict:
        """伴侣 >= 8.0.0：查询直播间开放数据（目前含总点赞数）。"""
        return self.request("getOpenRoomData")

    # -- 事件回调注册 --------------------------------------------------------
    def set_event_handler(self, on_event: OnEvent) -> None:
        self._on_event = on_event

    def set_frame_sink(self, on_frame: OnFrame) -> None:
        """仅 Mock 模式使用：本机"伴侣画布"的收帧钩子。"""
        self._on_frame = on_frame

    _pending: dict
    _on_event: OnEvent | None
    _on_frame: OnFrame | None


# ---------------------------------------------------------------------------
# Mock 实现：内存管道 + 伴侣行为轻量仿真（本地端到端自测用）
# ---------------------------------------------------------------------------
class MockPipeClient(PipeClient):
    """不依赖直播伴侣的完整链路仿真。

    - 响应 createMediaSources / x.subscribeEvent 等请求（与伴侣行为一致）
    - write_packet 把帧交给注册的 frame_sink（相当于"画布收到素材"）
    - inject_event() 可外部灌入 OPEN_LIVE_DATA 事件，模拟调试面板"模拟互动消息"
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending = {}
        self._on_event = None
        self._on_frame = None
        self._thread: threading.Thread | None = None
        self._rx: queue.Queue[dict] = queue.Queue()
        self.frames_sent = 0
        self.max_channels = 6
        self._next_channel = 1     # 通道 0 默认已存在

    def connect(self, pipe_name: str = "mock", max_channels: int = 6) -> bool:
        self.max_channels = max_channels
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="mock-partner")
        self._thread.start()
        return True

    def shutdown(self) -> None:
        self._rx.put({"__shutdown__": True})
        self._mark_disconnected()

    # -- 帧 -----------------------------------------------------------------
    def write_packet(self, pkt: Packet, timeout_ms: int = 1000) -> bool:
        if not self.is_connected():
            return False
        if pkt.video is not None and (pkt.video.cx > 1920 or pkt.video.cy > 1080):
            raise ValueError("单个素材通道分辨率不能超过 1920*1080")
        self.frames_sent += 1
        if self._on_frame:
            self._on_frame(pkt)
        return True

    # -- JSON 指令 -----------------------------------------------------------
    def send_message(self, payload: dict) -> None:
        self._rx.put(payload)

    def inject_event(self, event_name: str, params: dict) -> None:
        """仿真伴侣推送事件（如 OPEN_LIVE_DATA）。"""
        if self._on_event:
            self._on_event(event_name, params)

    def disconnect_from_partner(self) -> None:
        """仿真伴侣侧主动关闭：插件应收到 EVENT_DISCONNECTED 并退出。"""
        self._mark_disconnected()

    # -- 伴侣侧响应线程 ------------------------------------------------------
    def _serve(self) -> None:
        while True:
            try:
                msg = self._rx.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg.get("__shutdown__"):
                return
            if msg.get("type") != "request":
                continue
            req_id, method, params = msg.get("reqId"), msg.get("method"), msg.get("params", {})
            resp: dict = {"type": "request", "reqId": req_id, "code": 1,
                          "data": {}, "message": ""}
            if method == "createMediaSources":
                n = int(params.get("count", 1))
                ids = []
                for _ in range(n):
                    if self._next_channel >= self.max_channels:
                        resp = {"type": "request", "reqId": req_id, "code": 0,
                                "data": {}, "message": "超出 maxChannels 限制"}
                        break
                    ids.append({"name": f"素材{self._next_channel}",
                                "id": self._next_channel})
                    self._next_channel += 1
                resp["data"] = {"sourceIds": ids}
            elif method in ("x.subscribeEvent", "x.unsubscribeEvent",
                            "x.publishEvent"):
                resp["data"] = {}
            elif method == "getOpenGiftPools":
                from .gifts import default_gift_pool
                resp["data"] = {"gift_pool": default_gift_pool()}
            elif method == "getOpenRoomData":
                resp["data"] = {"room_like_count": 1024}
            elif method in ("removeMediaSources", "removeAllMediaSources",
                            "getCurrentMediaSources"):
                resp["data"] = {"sourceIds": []}
            cb = self._pending.pop(req_id, None)
            if cb:
                cb(resp)


# ---------------------------------------------------------------------------
# Native 实现：直接读写 Windows 命名管道（帧协议与官方 IPC_PACKET 对齐）
# ---------------------------------------------------------------------------
class NativePipeClient(PipeClient):
    r"""真实伴侣模式。

    伴侣启动插件时给出 ``--pipeName=N``，管道地址为 ``\\.\pipe\N``。
    官方 SDK（PipeSDK.dll）负责帧的序列化；本实现按同一帧语义直接走
    ``CreateFileW + WriteFile/ReadFile``，把 ``pack_frame()`` 的字节流写入管道。

    ⚠ 若严格校验要求必须使用官方 SDK（如提审版本），推荐两种落地方式：
      1) 用官方 C++ Demo 做壳（链 PipeSDK.dll），本机 localhost 把帧/事件
         转发给本 Python 进程（把本类换成 LocalBridgeClient 即可）；
      2) ctypes 加载 PipeSDK.dll 调 CreatePipeClient/WritePacket/SendMessage。
    两种方式都不需要改动上层业务模块。
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending = {}
        self._on_event = None
        self._on_frame = None
        self._handle = None
        self._rx_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def connect(self, pipe_name: str, max_channels: int = 6) -> bool:
        import ctypes
        from ctypes import wintypes
        target = pipe_name if pipe_name.startswith("\\\\") else rf"\\.\pipe\{pipe_name}"
        GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.restype = wintypes.HANDLE
        h = CreateFileW(target, GENERIC_READ | GENERIC_WRITE, 0, None,
                        OPEN_EXISTING, 0, None)
        INVALID = wintypes.HANDLE(-1).value
        if not h or ctypes.c_void_p(h).value == ctypes.c_void_p(INVALID).value:
            return False
        self._handle = h
        self._rx_thread = threading.Thread(target=self._read_loop, daemon=True,
                                           name="partner-rx")
        self._rx_thread.start()
        return True

    def shutdown(self) -> None:
        import ctypes
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        self._mark_disconnected()

    # -- 帧 -----------------------------------------------------------------
    def write_packet(self, pkt: Packet, timeout_ms: int = 1000) -> bool:
        import ctypes
        if not self._handle or not self.is_connected():
            return False
        buf = pack_frame(pkt)
        written = ctypes.c_ulong(0)
        with self._lock:
            ok = ctypes.windll.kernel32.WriteFile(
                self._handle, buf, len(buf), ctypes.byref(written), None)
        return bool(ok) and written.value == len(buf)

    # -- JSON 指令 -----------------------------------------------------------
    def send_message(self, payload: dict) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        import ctypes
        written = ctypes.c_ulong(0)
        with self._lock:
            ctypes.windll.kernel32.WriteFile(
                self._handle, raw, len(raw), ctypes.byref(written), None)

    # -- 接收循环：JSON 行（指令响应/事件）与帧字节流复用同一条管道，
    #    以首 4 字节魔数区分（对应官方 EVENT_MESSAGE 分发逻辑）。
    def _read_loop(self) -> None:
        import ctypes
        buf = b""
        chunk = ctypes.create_string_buffer(65536)
        got = ctypes.c_ulong(0)
        while self.is_connected() and self._handle:
            ok = ctypes.windll.kernel32.ReadFile(
                self._handle, chunk, 65536, ctypes.byref(got), None)
            if not ok or got.value == 0:
                # 管道断开 = 官方 EVENT_DISCONNECTED，预期插件自行退出
                self._mark_disconnected()
                return
            buf += chunk.raw[:got.value]
            while True:
                if buf.startswith(_FRAME_MAGIC):
                    if len(buf) < 8:
                        break
                    (meta_len,) = struct.unpack_from("<I", buf, 4)
                    dpos = 8 + meta_len
                    if len(buf) < dpos + 4:
                        break
                    (dlen,) = struct.unpack_from("<I", buf, dpos)
                    total = dpos + 4 + dlen
                    if len(buf) < total:
                        break
                    pkt = unpack_frame(buf[:total])
                    buf = buf[total:]
                    if self._on_frame:
                        self._on_frame(pkt)
                else:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl], buf[nl + 1:]
                    if line.strip():
                        self._dispatch_json(line)

    def _dispatch_json(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("type") == "event" and self._on_event:
            self._on_event(msg.get("eventName", ""), msg.get("params", {}))
        elif msg.get("type") == "request":
            cb = self._pending.pop(msg.get("reqId"), None)
            if cb:
                cb(msg)
