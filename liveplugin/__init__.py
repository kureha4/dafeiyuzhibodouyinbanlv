"""抖音直播伴侣 AI 虚拟主播互动插件 · Python 业务层

架构（对齐官方"互动插件(exe 型) + 命名管道"模型）：

    直播伴侣(官方客户端/画布/素材区/开放数据源)
        ▲ 音视频帧 ↑              │ 互动事件 + API 指令 ↓(JSON)
        │                        │
    [传输抽象 ipc.PipeClient]
        ├─ MockPipeClient   内存实现，无伴侣即可端到端跑通与验证
        ├─ NativePipeClient 桥接官方 PipeSDK.dll(命名管道)的占位，接真实伴侣时启用
        │
    业务模块:
        events.EventCenter   解析 OPEN_LIVE_DATA → 统一 LiveEvent(去重/点赞聚合/礼物连击)
        render.character     循环透明人物动画 (RGBA 帧)
        render.danmaku       装修版弹幕墙 (RGBA 帧)
        ai.AIOrchestrator    事件→提示词→远程大模型→回复 (TTS 接口留占位)
        main                 组装 + 渲染循环 + 伴侣启动参数解析
"""

from .ipc import PipeClient, MockPipeClient, NativePipeClient
from .events import EventCenter, LiveEvent, MsgType
from .gifts import GiftCatalog
from .config import Config

__all__ = [
    "PipeClient", "MockPipeClient", "NativePipeClient",
    "EventCenter", "LiveEvent", "MsgType",
    "GiftCatalog", "Config",
]
