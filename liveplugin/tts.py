"""本地 GPT-SoVITS TTS 接入（api_v2，默认 127.0.0.1:9880）。

- speak(text) 只入队；后台单线程串行合成（api_v2 内部本来就是全局锁，
  多端并发也只会排队，这里直接一个队列对齐它的节奏）。
- 合成结果 wav：本机 winsound 即时播放（控制台预览用），
  native 模式再转 48k 立体声 s16le PCM 推伴侣音频通道。
- 首次合成冷启动约 18-28 秒，稳态 5-8 秒；timeout 默认 120s。
- 全部参数在 config.json 的 `tts` 段，控制台可热改。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
import wave
from array import array
from io import BytesIO
from pathlib import Path


def wav_to_pcm(wav: bytes, rate: int = 48000) -> bytes:
    """wav → 伴侣音频通道要求的 48k 立体声 s16le PCM（单声道复制+线性重采样）。"""
    with wave.open(BytesIO(wav), "rb") as w:
        nch, sw, src_rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"只支持 16bit wav，实际 {sw*8}bit")
    a = array("h")
    a.frombytes(raw)
    if nch == 1:                                  # 单声道→立体声
        a = array("h", [v for pair in zip(a, a) for v in pair])
    elif nch > 2:                                 # 取前两声道
        out = array("h")
        for i in range(0, len(a) - nch + 1, nch):
            out.extend(a[i:i + 2])
        a = out
    if src_rate != rate:                          # 线性插值重采样
        frames = int(len(a) // 2 * rate / src_rate)
        out = array("h", bytes(frames * 2 * 2))   # frames×2ch，每短整型 2 字节
        maxf = len(a) // 2 - 1
        for i in range(frames):
            pos = i * src_rate / rate
            j = int(pos)
            f = pos - j
            j2 = j + 1 if j + 1 <= maxf else j
            for c in range(2):
                out[i * 2 + c] = int(a[j * 2 + c] * (1 - f) + a[j2 * 2 + c] * f)
        a = out
    return a.tobytes()


class TtsService:
    def __init__(self, cfg_getter, on_wav=None, log=print) -> None:
        self._cfg = cfg_getter          # callable -> 当前 tts 配置（热更新）
        self._on_wav = on_wav           # wav bytes -> 原生音频通道（可选）
        self._log = log
        self.stats = {"requested": 0, "done": 0, "failed": 0,
                      "dropped": 0, "last_ms": 0, "warm_ms": 0}
        self._q: queue.Queue[str] = queue.Queue(maxsize=24)
        self._tmp = (Path(__file__).resolve().parent.parent / "preview" / "tts")
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._tmp_i = 0
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="tts-worker")
        self._worker.start()
        threading.Thread(target=self._warm, daemon=True, name="tts-warm").start()

    def _warm(self) -> None:
        """开播前自动预热：消除 GPT-SoVITS 首次 18~28 秒的冷启动。"""
        time.sleep(2.0)
        if not self.rules().get("enabled"):
            return
        t0 = time.time()
        try:
            self.synthesize("你好呀。")
            self.stats["warm_ms"] = int((time.time() - t0) * 1000)
            self._log(f"[tts] 预热完成（{self.stats['warm_ms']}ms），"
                      "首条回复不再冷启动")
        except Exception as e:
            self._log(f"[tts] 预热失败（不影响使用）：{e.__class__.__name__}: {e}")

    # -- 配置 -------------------------------------------------------------
    def rules(self) -> dict:
        r = dict(self._cfg() or {})
        r.setdefault("enabled", False)
        r.setdefault("base_url", "http://127.0.0.1:9880")
        r.setdefault("ref_audio_path", "")
        r.setdefault("prompt_text", "")
        r.setdefault("prompt_lang", "zh")
        r.setdefault("text_lang", "auto")
        r.setdefault("speed_factor", 1.0)
        r.setdefault("seed", -1)
        r.setdefault("timeout", 120.0)
        r.setdefault("volume", 100)          # 语音音量 0-200（100=原始响度）
        r.setdefault("local_playback", True)
        r.setdefault("speak_types", ["reply", "member", "follow", "like"])
        return r

    def want(self, etype: str) -> bool:
        r = self.rules()
        return bool(r.get("enabled")) and \
            etype in (r.get("speak_types") or [])

    # -- 入口 -------------------------------------------------------------
    def speak(self, text: str) -> None:
        """入队合成；队列满直接丢弃（直播优先，不积压）。"""
        text = str(text or "").strip()
        if not text or not self.rules().get("enabled"):
            return
        self.stats["requested"] += 1
        try:
            self._q.put_nowait(text)
        except queue.Full:
            # 队列满：丢掉最旧的（旧的回复对直播已无意义），保住最新这条
            try:
                self._q.get_nowait()
                self._q.put_nowait(text)
                self.stats["dropped"] += 1
            except Exception:
                self.stats["failed"] += 1

    def test(self, text: str) -> dict:
        """同步合成一条（控制台试听）；返回耗时与字节数。"""
        t0 = time.time()
        try:
            wav = self.synthesize(text)
        except Exception as e:
            self.stats["failed"] += 1
            detail = f"{e.__class__.__name__}: {e}"
            try:                       # HTTPError 带 GPT-SoVITS 的 JSON 错误体
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"ok": False, "message": f"TTS 失败：{detail}"}
        ms = int((time.time() - t0) * 1000)
        self.stats["done"] += 1
        self.stats["last_ms"] = ms
        self._output(wav)
        return {"ok": True, "ms": ms, "bytes": len(wav)}

    # -- 合成 -------------------------------------------------------------
    def synthesize(self, text: str) -> bytes:
        r = self.rules()
        payload = {
            "text": text,
            "text_lang": str(r.get("text_lang") or "auto"),
            "ref_audio_path": str(r.get("ref_audio_path", "")).replace("\\", "/"),
            "prompt_text": str(r.get("prompt_text", "")),
            "prompt_lang": str(r.get("prompt_lang") or "zh"),
            "seed": int(r.get("seed", -1)),
            "speed_factor": float(r.get("speed_factor", 1.0)),
            "media_type": "wav",
            "streaming_mode": False,
        }
        req = urllib.request.Request(
            str(r.get("base_url", "")).rstrip("/") + "/tts",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req,
                                    timeout=float(r.get("timeout", 120))) as resp:
            return resp.read()

    # -- 后台队列 ---------------------------------------------------------
    def _loop(self) -> None:
        while True:
            text = self._q.get()
            t0 = time.time()
            try:
                wav = self.synthesize(text)
                self.stats["done"] += 1
                self.stats["last_ms"] = int((time.time() - t0) * 1000)
                self._output(wav)
            except Exception as e:
                self.stats["failed"] += 1
                self._log(f"[tts] 合成失败（本条跳过）：{e.__class__.__name__}: {e}")

    def _output(self, wav: bytes) -> None:
        vol = int(float(self.rules().get("volume", 100) or 0))
        vol = max(0, min(200, vol))
        if vol != 100:
            wav = self._gain(wav, vol / 100.0)
        if self._on_wav is not None:
            try:
                self._on_wav(wav)
            except Exception as e:
                self._log(f"[tts] 音频通道推送失败：{e}")
        if self.rules().get("local_playback", True):
            self._play(wav)

    @staticmethod
    def _gain(wav: bytes, g: float) -> bytes:
        """对 16bit wav 做线性增益（0~2 倍），纯标准库。"""
        try:
            with wave.open(BytesIO(wav), "rb") as w:
                params = w.getparams()
                raw = w.readframes(w.getnframes())
            a = array("h")
            a.frombytes(raw[:len(raw) & ~1])
            for i in range(len(a)):
                v = int(a[i] * g)
                a[i] = -32768 if v < -32768 else (32767 if v > 32767 else v)
            out = BytesIO()
            with wave.open(out, "wb") as w:
                w.setparams(params)
                w.writeframes(a.tobytes())
            return out.getvalue()
        except Exception:
            return wav

    # -- 本机试听 ---------------------------------------------------------
    def _play(self, wav: bytes) -> None:
        try:
            import winsound
            self._tmp_i = (self._tmp_i + 1) % 6
            p = self._tmp / f"tts_{self._tmp_i}.wav"
            p.write_bytes(wav)
            winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
            old = self._tmp / f"tts_{(self._tmp_i + 1) % 6}.wav"
            try:
                old.unlink()
            except OSError:
                pass                       # 还在播，下轮再删
        except Exception as e:
            self._log(f"[tts] 本机播放失败：{e}")
