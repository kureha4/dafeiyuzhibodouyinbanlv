"""AI 对话编排：互动事件 → 提示词 → 远程大模型 → 回复文本。

设计：
- 单 worker 线程消费事件队列（同一时刻只有一个在途请求，天然限流）
- 按事件类型节流（config.ai.min_interval），点赞/关注默认不进对话，走模板
- 礼物、进场(暂缺)→ 模板直出（"感谢xx送的小心心"），保证 0 延迟
- 评论（弹幕）→ 大模型生成个性化回复；完全异步：慢也照播，失败只记日志不上假模板
- 回复通过 callback 交回业务层（上弹幕墙 + 驱动人物 speaking 状态 + 未来 TTS）
- TTS 仅留 TtsProvider 抽象占位（按要求后置实现）
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request

from .events import LiveEvent

ReplyCb = "Callable[[str, LiveEvent], None]"


class TtsProvider:
    """TTS 抽象占位（后置实现）。

    实现约定：synthesize(text) -> PCM bytes(48k 立体声 s16le)，
    业务层拿到后 Packet.make_audio 推通道，或本机播放。
    """

    def synthesize(self, text: str) -> bytes | None:
        return None


class AIOrchestrator:
    def __init__(self, cfg: dict, on_reply, character=None,
                 on_usage=None) -> None:
        self.cfg = cfg or {}
        self.on_reply = on_reply                 # (text, event) -> None
        self.character = character               # 可选：驱动说话/鞠躬动画
        self.on_usage = on_usage                 # (prompt_tokens, completion_tokens)
        self.persona = self.cfg.get(
            "persona", "你是抖音直播间的 AI 虚拟主播，回复简短口语化。")
        self.welcome_tpl = self.cfg.get("welcome_template", "欢迎 {nickname}！")
        self.gift_tpl = self.cfg.get(
            "gift_template", "感谢 {nickname} 送的{gift_name}x{gift_num}！")
        self.min_interval = self.cfg.get("min_interval", {})
        self.history_size = int(self.cfg.get("history_size", 12))
        self.max_reply_chars = int(self.cfg.get("max_reply_chars", 60))
        self.timeout = float(self.cfg.get("request_timeout", 12.0))
        # 推理型模型要先想再答：token 预算必须给够，否则正文会是空串
        self.max_tokens = int(self.cfg.get("max_tokens", 500))
        self.temperature = float(self.cfg.get("temperature", 0.8))
        self.enabled = bool(self.cfg.get("enabled", True))
        self.tts = TtsProvider()

        self._q: queue.Queue[LiveEvent] = queue.Queue(
            maxsize=int(self.cfg.get("max_queue", 64)))
        self._last_fire: dict[str, float] = {}
        self._history: list[dict] = []
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._inflight = 0

    # -- 入口 -----------------------------------------------------------------
    def submit(self, ev: LiveEvent) -> None:
        # 推送白名单：只有列出的事件类型交给 AI 回复
        # （默认=弹幕评论+送礼；进场/关注/点赞等不推 AI，可自行接 TTS）
        types = self.cfg.get("reply_types") or ("comment", "gift")
        if ev.type not in types:
            return
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            pass                                  # 队列满直接丢弃，保直播流畅

    def start(self) -> None:
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="ai-worker")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # -- 主循环 ----------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._handle(ev)

    def _handle(self, ev: LiveEvent) -> None:
        interval = float(self.min_interval.get(ev.type, 0))
        now = time.time()
        if interval and now - self._last_fire.get(ev.type, 0) < interval:
            return
        self._last_fire[ev.type] = now

        if self.enabled and ev.type in ("comment", "gift"):
            # 弹幕和送礼交给大模型生成回复：完全异步——慢一点也照播，
            # 不丢回复、也不再用“收到xx的弹幕”这种模板糊弄观众
            self._ask_llm(ev)
        elif ev.type == "gift":
            # 大模型关闭：礼物仍用感谢模板
            text = self.gift_tpl.format(
                nickname=ev.nickname, gift_name=ev.gift_name,
                gift_num=ev.gift_num)
            self._deliver(text, ev, gesture="thanks")
        elif ev.type == "member":
            # 仅当 reply_types 里列了 member 才会走到这里（默认不推 AI）
            text = self.welcome_tpl.format(nickname=ev.nickname)
            self._deliver(text, ev, gesture="speaking")
        elif ev.type == "comment":
            # AI 关闭时弹幕不硬凑回复，只上墙
            pass
        elif ev.type == "follow":
            self._deliver(f"{ev.nickname} 关注了主播，感谢支持～", ev,
                          gesture="speaking")
        elif ev.type == "like":
            self._deliver(f"感谢 {ev.nickname} 的点赞支持～", ev,
                          gesture="speaking")
        # 白名单之外 / 其余类型：不生成回复，只进弹幕墙

    def _deliver(self, text: str, ev: LiveEvent, gesture: str = "speaking") -> None:
        if not text:
            return
        text = text[: self.max_reply_chars]
        est_seconds = max(1.5, len(text) / 5.0)    # 中文约 5 字/秒
        if self.character:
            self.character.set_state(gesture, est_seconds)
        self.on_reply(text, ev)

    # -- 大模型调用（OpenAI 兼容，纯标准库；完全异步，慢也不丢回复）-------------
    def _ask_llm(self, ev: LiveEvent) -> None:
        base = self.cfg.get("base_url", "").rstrip("/")
        key = self.cfg.get("api_key", "")
        model = self.cfg.get("model", "")
        if not (base and key and model):
            print("[ai] 未配置 base_url/api_key/model，无法生成回复")
            # 演示模式（如 CLI mock）没配密钥时给个可见的占位回复；
            # 真实配置了密钥就永远不会走到这里
            self._deliver(self._fallback(ev), ev, gesture="speaking")
            return
        with self._lock:
            if self._inflight >= 3:      # 在途请求上限，防止刷爆接口
                return
            self._inflight += 1

        if ev.type == "gift":
            line = (f"[{ev.nickname} 送礼物] 送了 {ev.gift_name} ×{ev.gift_num}，"
                    "请热情感谢这位观众")
        else:
            line = f"[{ev.nickname}] {ev.content}"
        self._history.append({"role": "user", "content": line})
        msgs = [{"role": "system",
                 "content": self.persona.format(
                     char_name=self.cfg.get("char_name", "小抖"))}]
        msgs += self._history[-self.history_size:]
        body = json.dumps({
            "model": model,
            "messages": msgs,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})

        def _call() -> None:
            text = ""
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                usage = data.get("usage") or {}
                if self.on_usage:
                    try:
                        self.on_usage(int(usage.get("prompt_tokens", 0) or 0),
                                      int(usage.get("completion_tokens", 0) or 0))
                    except Exception:
                        pass
                msg = ((data.get("choices") or [{}])[0]).get("message") or {}
                c = msg.get("content")
                if isinstance(c, list):          # 部分网关返回分段数组
                    c = "".join(p.get("text", "") if isinstance(p, dict)
                                else str(p) for p in c)
                text = (c or "").strip()
                if not text:
                    print("[ai] 模型返回空正文（推理模型请把 max_tokens 调大，"
                          f"当前={self.max_tokens}；thinking 前80字="
                          f"{(msg.get('reasoning_content') or '')[:80]!r}）")
            except Exception as e:
                print(f"[ai] 调用失败({e.__class__.__name__}: {e})")
            finally:
                with self._lock:
                    self._inflight -= 1
            if text:
                self._history.append({"role": "assistant", "content": text})
                self._deliver(text, ev,
                              gesture="thanks" if ev.type == "gift"
                              else "speaking")
        threading.Thread(target=_call, daemon=True, name="ai-http").start()

    def _fallback(self, ev: LiveEvent) -> str:
        """仅用于没配密钥的离线演示；配好密钥后失败只记日志，不再上假回复。"""
        if ev.type == "gift":
            return self.gift_tpl.format(nickname=ev.nickname,
                                        gift_name=ev.gift_name,
                                        gift_num=ev.gift_num)
        return f"收到 {ev.nickname} 的弹幕～"
