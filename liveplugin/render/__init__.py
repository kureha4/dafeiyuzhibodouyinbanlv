"""渲染层：把画面生成为 RGBA 帧推送到素材通道。"""
from .balance import BalanceRenderer
from .bubble import BubbleRenderer
from .character import CharacterRenderer
from .danmaku import DanmakuRenderer
from .histo import HistoRenderer
from .songq import SongQueueRenderer

__all__ = ["BalanceRenderer", "BubbleRenderer", "CharacterRenderer",
           "DanmakuRenderer", "HistoRenderer", "SongQueueRenderer"]
