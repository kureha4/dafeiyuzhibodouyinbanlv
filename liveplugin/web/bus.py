"""事件总线：把运行中的事件、AI 回复、日志汇聚成可订阅的流。

- publish(kind, data)：业务线程调用（事件监听器 / wss 日志 / 回复回调）
- subscribe()：SSE 处理器挂一个队列，先收历史快照再收增量
- history()：最近 N 条事件+回复，供新连接的页面回放
纯内存、线程安全，零依赖。
"""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from collections import deque

_seq = itertools.count(1)


class Bus:
    def __init__(self, history_size: int = 300, log_size: int = 200) -> None:
        self._history: deque[dict] = deque(maxlen=history_size)
        self._logs: deque[dict] = deque(maxlen=log_size)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    # -- 发布 -----------------------------------------------------------------
    def publish(self, kind: str, data: dict) -> None:
        item = {"id": next(_seq), "kind": kind, "ts": time.time(), "data": data}
        with self._lock:
            if kind in ("event", "reply"):
                self._history.append(item)
            if kind == "log":
                self._logs.append(item)
            for q in list(self._subs):
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass        # 慢消费者丢帧，别拖垮业务线程

    def log(self, text: str, level: str = "info") -> None:
        self.publish("log", {"level": level, "text": str(text)})

    # -- 订阅 -----------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def logs(self) -> list[dict]:
        with self._lock:
            return list(self._logs)


def sse_encode(item: dict) -> bytes:
    return f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8")
