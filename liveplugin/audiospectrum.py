"""音频频谱采集：WASAPI 环回录制本机扬声器输出 → FFT 分成 N 个频段电平。

抓的是「音箱里真正发出的声音」——BGM、点歌、语音、音效都会驱动频谱，
完全静音时直方图落回 0（不做假跳动）。依赖 pyaudiowpatch + numpy（pip 装好）。

用法：
    src = SpectrumSource(bars=28)      # 后台线程持续采集
    src.levels()                       # 最近一帧的 N 个频段电平 0.0~1.0
    src.available                      # 环回采集是否活着（失败自动重试打开）
"""
from __future__ import annotations

import threading
import time

import numpy as np


class SpectrumSource:
    CHUNK = 2048

    def __init__(self, bars: int = 28, sensitivity: float = 1.0) -> None:
        self.bars = max(8, min(64, int(bars)))
        self.sensitivity = max(0.2, min(3.0, float(sensitivity)))
        self.available = False
        self.last_error = ""
        self._levels = np.zeros(self.bars, dtype=np.float32)
        self._peaknorm = 0.05            # 自动增益参考峰值
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="spectrum")
        self._thread.start()

    def levels(self) -> list[float]:
        with self._lock:
            return self._levels.tolist()

    def set_bars(self, bars: int) -> None:
        bars = max(8, min(64, int(bars)))
        if bars != self.bars:
            self.bars = bars
            with self._lock:
                self._levels = np.zeros(bars, dtype=np.float32)

    def stop(self) -> None:
        self._stop.set()

    # -- 采集线程 ---------------------------------------------------------------
    def _loop(self) -> None:
        stream = None
        pa = None
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            self.last_error = f"pyaudiowpatch 未安装: {e}"
            return
        while not self._stop.is_set():
            try:
                if stream is None:
                    pa = pyaudio.PyAudio()
                    lb = list(pa.get_loopback_device_info_generator())
                    if not lb:
                        raise RuntimeError("没有找到环回录音设备")
                    d = lb[0]
                    sr = int(d["defaultSampleRate"])
                    stream = pa.open(rate=sr,
                                     channels=d["maxInputChannels"],
                                     format=pyaudio.paInt16, input=True,
                                     input_device_index=d["index"],
                                     frames_per_buffer=self.CHUNK)
                    self._band_edges = np.geomspace(40.0, sr / 2.4, self.bars + 1)
                    self._win = np.hanning(self.CHUNK).astype(np.float32)
                    self._sr = sr
                raw = stream.read(self.CHUNK, exception_on_overflow=False)
                self._feed(raw)
                self.available = True
                self.last_error = ""
            except Exception as e:
                self.available = False
                self.last_error = str(e)[:120]
                try:
                    if stream is not None:
                        stream.stop_stream(); stream.close()
                except Exception:
                    pass
                stream = None
                try:
                    if pa is not None:
                        pa.terminate()
                except Exception:
                    pass
                pa = None
                self._stop.wait(2.0)     # 设备拔出/占用：2 秒后重开

    def _feed(self, raw: bytes) -> None:
        if getattr(self, "_band_edges", None) is None or \
                self._band_edges.size != self.bars + 1:
            self._band_edges = np.geomspace(40.0, self._sr / 2.4, self.bars + 1)
            self._win = np.hanning(self.CHUNK).astype(np.float32)
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # 双声道交织 → 单声道（环回常见立体声）
        if a.size == self.CHUNK * 2:
            a = a.reshape(-1, 2).mean(axis=1)
        if a.size != self.CHUNK:
            return
        rms = float(np.sqrt((a * a).mean()))
        with self._lock:
            cur = self._levels
            if rms < 0.002:              # 静音：电平快速落回 0
                self._levels = cur * 0.75
                return
            sp = np.abs(np.fft.rfft(a * self._win))
            freqs = np.fft.rfftfreq(self.CHUNK, 1.0 / self._sr)
            edges = self._band_edges
            out = np.empty(self.bars, dtype=np.float32)
            for i in range(self.bars):
                seg = sp[(freqs >= edges[i]) & (freqs < edges[i + 1])]
                m = float(seg.mean()) if seg.size else 0.0
                # 对数压缩：小声音也看得见，大声音不炸顶
                out[i] = np.log1p(m * 6.0)
            # 自动增益：参考峰值缓慢衰减，突发不会把画面顶满
            self._peaknorm = max(float(out.max()), self._peaknorm * 0.995, 0.15)
            out = np.clip(out / self._peaknorm * self.sensitivity, 0.0, 1.0)
            # 快起慢落：柱子上冲直接、下落平滑（视觉上“跳动”而非“抖动”）
            up = np.maximum(out, cur * 0.78 + 0.002)
            self._levels = up
