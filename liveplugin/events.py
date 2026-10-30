"""互动事件中心：订阅 OPEN_LIVE_DATA，解析并归一化为 LiveEvent 总线。

对齐官方事件结构（2024.04.01 调整后的 payload）：
msg_type: 1=点赞 live_like, 2=评论 live_comment, 3=礼物 live_gift,
          4=粉丝团变更 live_fansclub, 5=关注变更 live_follow

处理要点：
- msg_id 幂等去重（官方已做单条点赞幂等，此处防重连重放）
- 点赞二次聚合：同一 sec_open_id 在 like_window 内累加 like_num
- 礼物连击合并：同一 sec_open_id + sec_gift_id 在 combo_window 内累加 gift_num
- 解析后通过监听器分发给渲染层与 AI 层
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from .gifts import GiftCatalog


class MsgType(IntEnum):
    LIKE = 1
    COMMENT = 2
    GIFT = 3
    FANSCLUB = 4
    FOLLOW = 5
    MEMBER = 6          # WSS 逆向：进入直播间
    ROOM_STATS = 7      # WSS 逆向：在线人数


@dataclass
class LiveEvent:
    type: str                      # like / comment / gift / fansclub / follow
    msg_id: str = ""
    timestamp: float = 0.0         # 秒（由毫秒事件时间戳转换，或本地接收时间）
    user_id: str = ""              # sec_open_id（加密 openid）
    nickname: str = ""
    avatar_url: str = ""
    content: str = ""              # 评论内容
    gift_id: str = ""              # sec_gift_id
    gift_name: str = ""
    gift_num: int = 1
    gift_value: int = 0            # 单价（抖币）
    like_num: int = 0
    fansclub_reason: int = 0       # 1升级 2加团 16退团
    follow_action: int = 0         # 1关注 2取关
    privilege_level: int = 0       # 荣誉等级
    is_follow_anchor: bool = False
    fansclub_level: int = 0
    member_count: int = 0          # WSS 逆向进场消息携带的在线人数
    raw: dict = field(default_factory=dict)

    @property
    def total_value(self) -> int:
        return self.gift_value * self.gift_num


Listener = Callable[[LiveEvent], None]

_TYPE_BY_STR = {
    "live_like": "like",
    "live_comment": "comment",
    "live_gift": "gift",
    "live_fansclub": "fansclub",
    "live_follow": "follow",
    "live_member": "member",        # WSS 逆向独有：进入直播间
    "live_room_stats": "room_stats",
}


class EventCenter:
    def __init__(self, gift_catalog: GiftCatalog | None = None,
                 like_window: float = 2.0, combo_window: float = 3.0,
                 dedup_size: int = 4096) -> None:
        self.gifts = gift_catalog or GiftCatalog()
        self._listeners: list[Listener] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_size = dedup_size
        self._like_window = like_window
        self._combo_window = combo_window
        self._like_pending: dict[str, tuple[float, LiveEvent, Callable[[LiveEvent], None]]] = {}
        self._gift_pending: dict[str, tuple[float, LiveEvent, Callable[[LiveEvent], None]]] = {}
        self._lock = threading.Lock()
        self._timers: set[threading.Timer] = set()
        self.stats = {"received": 0, "dropped_dup": 0, "emitted": 0}

    # -- 对外接口 ------------------------------------------------------------
    def on_event(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def handle_partner_event(self, event_name: str, params: dict) -> None:
        """挂到 PipeClient.set_event_handler。"""
        if event_name == "OPEN_LIVE_DATA":
            for item in params.get("payload", []) or []:
                self._ingest(item)

    # -- 解析 ----------------------------------------------------------------
    def _ingest(self, item: dict) -> None:
        with self._lock:
            self.stats["received"] += 1
            msg_id = str(item.get("msg_id", ""))
            if msg_id:
                if msg_id in self._seen:
                    self.stats["dropped_dup"] += 1
                    return
                self._seen[msg_id] = None
                while len(self._seen) > self._dedup_size:
                    self._seen.popitem(last=False)

        tstr = item.get("msg_type_str") or _TYPE_BY_STR.get(
            {1: "live_like", 2: "live_comment", 3: "live_gift",
             4: "live_fansclub", 5: "live_follow"}.get(int(item.get("msg_type", 0)), ""), "")
        if tstr not in _TYPE_BY_STR:
            return
        etype = _TYPE_BY_STR[tstr]

        ts = float(item.get("timestamp", 0)) / 1000.0 or time.time()
        ev = LiveEvent(
            type=etype, msg_id=msg_id, timestamp=ts,
            user_id=str(item.get("sec_open_id", "")),
            nickname=item.get("nickname", "") or "观众",
            avatar_url=item.get("avatar_url", ""),
            content=item.get("content", ""),
            gift_id=item.get("sec_gift_id", ""),
            gift_num=int(item.get("gift_num", 1) or 1),
            like_num=int(item.get("like_num", 1) or 1),
            fansclub_reason=int(item.get("fansclub_reason_type", 0) or 0),
            follow_action=int(item.get("user_follow_action", 0) or 0),
            privilege_level=int(item.get("user_privilege_level", 0) or 0),
            is_follow_anchor=bool(item.get("is_follow_anchor", False)),
            fansclub_level=int(item.get("fansclub_level", 0) or 0),
            member_count=int(item.get("member_count", 0) or 0),
            raw=item,
        )
        if etype == "gift":
            plain = item.get("gift_name_plain", "")
            if plain:
                # WSS 逆向来源：协议内直接带礼物名（无 sec_gift_id）
                ev.gift_name = plain
            else:
                ev.gift_name = self.gifts.name_of(ev.gift_id)
            ev.gift_value = self.gifts.value_of(ev.gift_id) if ev.gift_id else 0

        now = time.time()
        if etype == "like":
            self._debounce(self._like_pending, ("like", ev.user_id), ev, now,
                           self._like_window)
        elif etype == "gift":
            self._debounce(self._gift_pending, ("gift", ev.user_id, ev.gift_id),
                           ev, now, self._combo_window)
        else:
            self._emit(ev)
        self._flush_stale(now)

    # -- 聚合 ----------------------------------------------------------------
    def _debounce(self, store: dict, key, ev: LiveEvent,
                  now: float, window: float) -> None:
        """滑动窗口聚合：窗口内同类事件累加；每个 key 只挂一个到期 Timer，
        到期时若事件仍在窗口内活跃则顺延，否则取出发送。"""
        with self._lock:
            if key in store:
                entry = store[key]
                entry["acc"].like_num += ev.like_num
                entry["acc"].gift_num += ev.gift_num
                entry["acc"].timestamp = now
                entry["last"] = now
                entry["deadline"] = now + window
                return
            entry = {"acc": ev, "last": now, "deadline": now + window}
            store[key] = entry
        t = threading.Timer(window, self._fire, args=(store, key, window))
        t.daemon = True
        with self._lock:
            self._timers.add(t)
        t.start()

    def _fire(self, store: dict, key, window: float) -> None:
        with self._lock:
            entry = store.get(key)
            if entry is None:
                return
            if entry["deadline"] > time.time():
                # 还在滑动窗口内，顺延到期再发
                remaining = entry["deadline"] - time.time()
                t = threading.Timer(remaining, self._fire, args=(store, key, window))
                t.daemon = True
                self._timers.add(t)
                t.start()
                return
            store.pop(key, None)
            ev = entry["acc"]
        self._emit(ev)

    def _flush_stale(self, now: float) -> None:
        """兜底：Timer 之外，直接发超期未触达的聚合（>10s 视为异常）。"""
        for store in (self._like_pending, self._gift_pending):
            with self._lock:
                expired = [k for k, e in store.items() if now - e["last"] > 10.0]
                popped = [(k, store.pop(k)) for k in expired]
            for _k, entry in popped:
                self._emit(entry["acc"])

    # -- 分发 ----------------------------------------------------------------
    def _emit(self, ev: LiveEvent) -> None:
        self.stats["emitted"] += 1
        for listener in self._listeners:
            try:
                listener(ev)
            except Exception:
                pass
