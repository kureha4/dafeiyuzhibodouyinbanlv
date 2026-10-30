"""礼物音效：收到礼物时播放一声「金币」之类的短音效。

音效文件由用户从外部添加（WebUI 上传或手动扔进 assets/），只支持 .wav
（Windows 原生异步播放，零依赖）。多个音效会随机挑一个播，避免听腻。

配置 config.sfx：
  enabled  开关（默认开）
  files    音效文件名列表（相对 assets/ 的文件名，或绝对路径）
"""
from __future__ import annotations

import random
import threading
from pathlib import Path


class GiftSfx:
    def __init__(self, cfg_getter, assets_dir, log=None) -> None:
        self.cfg_getter = cfg_getter
        self.assets = Path(assets_dir)
        self._log = log or (lambda m: print(f"[音效] {m}"))
        self._lock = threading.Lock()

    def files(self) -> list[Path]:
        cfg = self.cfg_getter() or {}
        if not cfg.get("enabled", True):
            return []
        out: list[Path] = []
        for f in (cfg.get("files") or []):
            p = Path(str(f))
            if not p.is_absolute():
                p = self.assets / p.name      # 只收 assets/ 下的（防路径穿越）
            if p.suffix.lower() == ".wav" and p.exists():
                out.append(p)
        return out

    def play(self) -> bool:
        """挑一个音效异步播放（在独立线程，不阻塞直播循环）。"""
        files = self.files()
        if not files:
            return False
        threading.Thread(target=self._play, args=(random.choice(files),),
                         daemon=True, name="gift-sfx").start()
        return True

    def _play(self, p: Path) -> None:
        try:
            import winsound
            # 从文件异步播放：不阻塞直播循环，多条音效互不打断
            winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            self._log(f"音效播放失败（{p.name}）：{e}")
