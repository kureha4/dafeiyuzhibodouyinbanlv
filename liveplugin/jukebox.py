"""点歌服务：弹幕「点歌 歌名」→ 排队 → 官方 ncm-cli 逐首播放。

排队规则：先进先出；每首只播 play_seconds 秒（默认 30），结束前 fade_seconds
秒内音量阶梯淡出 → stop → 播下一首。mpv 全局配置负责淡入（见 README）。

依赖（一次性）：
  npm install -g @music163/ncm-cli
  config set appId/privateKey（控制台卡片里填即可）
  ncm-cli login（扫一次登录链接）

配置 config.jukebox：
  enabled / bin / trigger / cooldown / blacklist
  queue_max      队列上限（满了拒绝新点歌）
  play_seconds   每首播放秒数（默认 30）
  fade_seconds   淡出时长（默认 1.5）
  voice          播报模板（{nickname} {song}）
"""
from __future__ import annotations

import json
import os
import queue
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

_TTS_TPL = "感谢 {nickname} 的点歌：《{song}》"

# 队列空了自动垫歌的默认 BGM 池（config.jukebox.bgm_pool 可换）
_BGM_POOL = ("起风了", "稻香", "晴天", "平凡之路", "夜空中最亮的星", "倔强",
             "遇见", "童话", "慢慢喜欢你", "漂洋过海来看你", "海阔天空",
             "光辉岁月", "记事本", "朋友", "红日", "后来")


class Jukebox:
    def __init__(self, cfg_getter, log, on_speak=None) -> None:
        self.cfg_getter = cfg_getter
        self.log = log
        self.on_speak = on_speak
        self._lock = threading.Lock()
        self._queue: list[dict] = []          # 等待播放
        self._current: dict | None = None     # 正在播放（含 started_at）
        self._worker: threading.Thread | None = None
        self._last_req: dict[str, float] = {}
        self._skip = threading.Event()        # 手动跳过当前
        self._recent: list[str] = []          # 最近播过的歌 id（BGM 防重复）
        self.stats = {"requests": 0, "played": 0, "failed": 0, "rejected": 0,
                      "bgm": 0}

    # -- 音量策略 ---------------------------------------------------------------
    def _music_vol(self) -> int:
        """背景音乐音量（恒定，不随说话变化），支持直播中实时调整。"""
        cfg = self.cfg_getter() or {}
        return max(0, min(100, int(float(cfg.get("music_volume", 45) or 0))))

    # -- CLI 基础 -------------------------------------------------------------
    def _base(self, cfg: dict) -> list[str]:
        b = str(cfg.get("bin") or "").strip()
        if b:
            return [b] if Path(b).exists() else b.split()
        appdata = os.environ.get("APPDATA") or ""
        cands = [Path(appdata) / "npm/node_modules/@music163/ncm-cli/dist/index.js",
                 Path.home() / "AppData/Roaming/npm/node_modules/@music163/ncm-cli/dist/index.js"]
        for c in cands:
            if c.exists() and shutil.which("node"):
                return ["node", str(c)]
        return ["ncm-cli"]

    def _cli(self, args: list[str], timeout: int = 30) -> tuple[bool, str]:
        cfg = self.cfg_getter() or {}
        # NCM_LEGACY_PLAY=1：走旧版直连播放。0.1.7 新增的播放器 daemon 在慢机器上
        # 3 秒超时无响应，导致 play 表面成功实际不出声（实测 legacy 模式全通）。
        env = dict(os.environ, NCM_LEGACY_PLAY="1")
        try:
            r = subprocess.run(self._base(cfg) + args, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, env=env)
            out = (r.stdout or "") + (r.stderr or "")
            return r.returncode == 0, out.strip()
        except FileNotFoundError:
            return False, "ncm-cli 未安装：npm install -g @music163/ncm-cli"
        except Exception as e:
            return False, str(e)

    def _wait_playing(self, max_wait: float = 15.0) -> bool:
        """play 之后核实播放器真的在播（防止 CLI 假成功、实际没出声）。
        mpv 冷启动 + 取播放地址可能要 10 秒左右，窗口要给足。"""
        t0 = time.time()
        while time.time() - t0 < max_wait:
            if self._playing():
                return True
            time.sleep(1.0)
        return False

    def _playing(self) -> bool:
        """播放器当前是否在播（读不到状态时按 True 处理，防误杀）。"""
        ok, out = self._cli(["state", "--output", "json"], timeout=10)
        if not ok:
            return True
        i = out.find("{")
        try:
            return ((json.loads(out[i:]).get("state") or {})
                    .get("status") == "playing")
        except Exception:
            return True

    def login(self) -> dict:
        ok, out = self._cli(["login", "--background", "--output", "json"],
                            timeout=30)
        url = ""
        try:
            d = json.loads(out[out.find("{"):])
            url = d.get("clickableUrl") or d.get("qrCodeUrl") or ""
        except Exception:
            pass
        self.log(f"登录链接: {url or out[:120]}")
        return {"ok": bool(url), "url": url, "message": out[:200]}

    def configure(self, appid: str, pkey: str) -> dict:
        ok, msgs = True, []
        if appid:
            o, m = self._cli(["config", "set", "appId", appid])
            ok = ok and o; msgs.append(m)
        if pkey:
            o, m = self._cli(["config", "set", "privateKey", "--", pkey])
            ok = ok and o; msgs.append(m)
        self.log(f"点歌密钥配置: {'成功' if ok else '失败'}")
        return {"ok": ok, "message": "；".join(x for x in msgs if x)[:300]}

    # -- 搜索 ------------------------------------------------------------------
    _KEYS_ID = ("encryptedId", "encrypted_id", "encryptSongId", "id")
    _KEYS_OID = ("originalId", "original_id", "songId")
    _KEYS_NAME = ("name", "songName", "song_name", "title")

    def search(self, keyword: str) -> list[dict]:
        ok, out = self._cli(["search", "song", "--keyword", keyword,
                             "--userInput", "直播间观众点歌", "--output", "json"])
        songs = self._parse(out) if ok and out else []
        if songs:
            # visible=false 的歌播不了，直接过滤
            return [s for s in songs if s.get("visible", True)]
        return []

    def _parse(self, out: str) -> list[dict]:
        i = min((x for x in (out.find("{"), out.find("[")) if x >= 0),
                default=-1)
        if i < 0:
            return []
        try:
            data = json.loads(out[i:])
        except Exception:
            return []
        data = self._find_songs(data) or []
        res = []
        for it in data[:10]:
            if not isinstance(it, dict):
                continue
            name = next((str(it[k]) for k in self._KEYS_NAME if it.get(k)), "")
            if not name:
                continue
            art = it.get("artists") or it.get("artist") or it.get("ar") or ""
            if isinstance(art, list):
                art = "/".join(str(a.get("name", a)) if isinstance(a, dict)
                               else str(a) for a in art)
            res.append({
                "name": name, "artist": str(art),
                "encrypted_id": next((str(it[k]) for k in self._KEYS_ID
                                      if it.get(k)), ""),
                "original_id": next((str(it[k]) for k in self._KEYS_OID
                                     if it.get(k)), ""),
                "visible": bool(it.get("visible", True)),
            })
        return res

    @classmethod
    def _find_songs(cls, o, depth: int = 0):
        """在任意嵌套的 JSON 里找到第一个歌曲列表（不同版本字段位置不同）。"""
        if isinstance(o, list):
            if (o and isinstance(o[0], dict)
                    and any(k in o[0] for k in cls._KEYS_NAME)
                    and any(k in o[0] for k in cls._KEYS_ID + cls._KEYS_OID)):
                return o
            return None
        if isinstance(o, dict) and depth < 4:
            for v in o.values():
                r = cls._find_songs(v, depth + 1)
                if r:
                    return r
        return None

    # -- 状态（面板/UI 共用） ---------------------------------------------------
    def panel(self) -> dict:
        cfg = self.cfg_getter() or {}
        ps = float(cfg.get("play_seconds", 30) or 30)
        cur = None
        if self._current:
            cur = dict(self._current)
            cur["elapsed"] = round(time.time() - cur.get("started_at",
                                                          time.time()), 1)
            cur["limit"] = 600 if cur.get("bgm") else int(ps)
        return {"playing": self._current is not None,
                "current": cur,
                "queue": [{k: s.get(k, "") for k in ("name", "artist", "nickname")}
                          for s in list(self._queue)],
                "requests": self.stats["requests"],
                "played": self.stats["played"],
                "bgm": self.stats["bgm"]}

    def state(self) -> dict:      # 兼容旧字段
        return self.panel()

    # -- 弹幕入口 ---------------------------------------------------------------
    def handle_comment(self, nickname: str, content: str) -> bool:
        cfg = self.cfg_getter() or {}
        if not cfg.get("enabled"):
            return False
        text = (content or "").strip()
        trig = str(cfg.get("trigger") or "点歌")
        kw = ""
        for prefix in (f"{trig}:", f"{trig}：", f"!{trig}", f"/{trig}", trig):
            if text.startswith(prefix):
                kw = text[len(prefix):].strip(" :：\u3000")
                break
        else:
            return False
        threading.Thread(target=self.request, args=(nickname, kw),
                         daemon=True, name="juke-req").start()
        return True

    def request(self, nickname: str, keyword: str) -> dict:
        cfg = self.cfg_getter() or {}
        self.stats["requests"] += 1
        keyword = (keyword or "").strip()

        def fail(msg: str, say: str = "") -> dict:
            self.log(f"点歌未成功: {msg}")
            if say and self.on_speak:
                self.on_speak(say)
            return {"ok": False, "message": msg}

        if not keyword:
            return fail("点歌格式：点歌 歌名", "想点歌的话，发点歌空格加歌名就可以啦")
        low = keyword.lower()
        for w in (cfg.get("blacklist") or []):
            w = str(w).strip().lower()
            if w and w in low:
                self.stats["rejected"] += 1
                return fail(f"《{keyword}》在黑名单里", "这首歌不能点哦，换一首吧")
        cd = float(cfg.get("cooldown", 60) or 0)
        now = time.time()
        last = self._last_req.get(nickname or "?", 0)
        if cd and now - last < cd:
            self.stats["rejected"] += 1
            return fail(f"冷却中（{int(cd - (now - last))}s 后可再点）",
                        "刚点过啦，稍等一会儿再点")
        with self._lock:
            qmax = int(cfg.get("queue_max", 8) or 8)
            if len(self._queue) >= qmax:
                self.stats["rejected"] += 1
                return fail(f"队满了（上限 {qmax} 首），改晚点再点",
                            "点歌排队满了，待会儿再点吧")
        self._last_req[nickname or "?"] = now
        songs = self.search(keyword)
        if not songs:
            self.stats["failed"] += 1
            return fail(f"没搜到《{keyword}》（可能要登录）",
                        f"没搜到{keyword}，换个歌名试试")
        pick = dict(songs[0]); pick["nickname"] = nickname or "观众"
        pick["requested_at"] = now
        with self._lock:
            self._queue.append(pick)
            pos = len(self._queue)
        self.stats["played"] += 0
        song_disp = pick["name"] + (f" - {pick['artist']}" if pick.get("artist") else "")
        self.log(f"点歌进队 #{pos}: {nickname} → 《{song_disp}》")
        with self._lock:
            cur = self._current
        if cur is not None and cur.get("bgm"):
            self._skip.set()        # BGM 给点歌让位：点歌马上接上播
        tpl = str(cfg.get("voice") or _TTS_TPL)
        if self.on_speak:
            try:
                self.on_speak(tpl.format(nickname=nickname or "观众",
                                         song=pick["name"]))
            except Exception:
                pass
        self._ensure_worker()
        return {"ok": True, "song": pick, "pos": pos, "matches": len(songs)}

    # -- 播放 worker：逐首播（观众歌只播 play_seconds 秒；BGM 播完整首） -----------
    def ensure_worker(self) -> None:
        """点歌启用时拉起播放线程（即使没歌也跑 BGM 循环，保证开播即有音乐）。"""
        if (self.cfg_getter() or {}).get("enabled"):
            self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._work, daemon=True,
                                            name="juke-play")
        self._worker.start()

    # -- 自动 BGM：队列空了随机垫一首保证音乐不停；来点歌立刻让位 -----------------
    def _bgm_on(self) -> bool:
        return bool((self.cfg_getter() or {}).get("bgm_enabled", True))

    def _bgm_pick(self) -> dict | None:
        """随机挑几个 BGM 关键词搜索，取第一首最近没播过的。"""
        cfg = self.cfg_getter() or {}
        pool = [str(x).strip() for x in (cfg.get("bgm_pool") or _BGM_POOL)
                if str(x).strip()]
        for kw in random.sample(pool, min(len(pool), 4)):
            for s in self.search(kw)[:6]:
                if (s.get("original_id") or "") in self._recent:
                    continue
                pick = dict(s)
                pick.update(nickname="自动BGM", bgm=True)
                self._recent.append(pick.get("original_id") or pick["name"])
                del self._recent[24:]
                self.stats["bgm"] += 1
                self.log(f"队列空了，自动垫 BGM《{pick['name']}》")
                return pick
        return None

    def _work(self) -> None:
        while True:
            with self._lock:
                queue_empty = not self._queue
                song = None if queue_empty else self._queue.pop(0)
                if not queue_empty:
                    song["started_at"] = time.time()
                    self._current = song
                    self._skip.clear()
            if queue_empty:
                if not self._bgm_on():        # BGM 关着 → 收工
                    with self._lock:
                        self._current = None
                        self._worker = None
                    return
                song = self._bgm_pick()      # 搜索要几秒，放在锁外
                if song is None:             # 搜不到（限流/登录失效）：10s 后重试
                    with self._lock:
                        self._current = None
                        self._worker = None
                    threading.Timer(10.0, self._ensure_worker).start()
                    return
                with self._lock:
                    if self._queue:          # 搜索期间来点歌：点歌优先，BGM 排队尾
                        self._queue.append(song)
                        song = self._queue.pop(0)
                    song["started_at"] = time.time()
                    self._current = song
                    self._skip.clear()
            cfg = self.cfg_getter() or {}
            ps = 600.0 if song.get("bgm") else \
                max(10.0, float(cfg.get("play_seconds", 30) or 30))
            args = ["play", "--song"]
            if song.get("encrypted_id"):
                args += ["--encrypted-id", song["encrypted_id"]]
            if song.get("original_id"):
                args += ["--original-id", song["original_id"]]
            # 计时从此刻起算；音量命令走独立小线程（ncm-cli 每次调用要 1~2s，
            # 绝不能阻塞计时循环，否则 30s 上限会被拖长）
            t0 = time.time()
            vq: queue.Queue = queue.Queue()

            def _vol_worker():
                while True:
                    a = vq.get()
                    if a is None:
                        return
                    self._cli(a, timeout=8)
            vt = threading.Thread(target=_vol_worker, daemon=True,
                                  name="juke-vol")
            vt.start()
            vq.put(["volume", str(self._music_vol())])  # 开播即恒定音量
            ok, msg = self._cli(args, timeout=30)
            if ok and not self._wait_playing():
                ok = False
                msg = "播放器未真正出声（daemon 无响应 / 接口限流）"
            if not ok:
                self.stats["failed"] += 1
                self.log(f"播放失败《{song['name']}》: {msg[:150]}")
                if self.on_speak:
                    self.on_speak("点歌服务有点忙，过一会儿再试点歌试试")
                vq.put(["volume", str(self._music_vol())]); vq.put(None)
                with self._lock:
                    self._current = None
                time.sleep(1)
                continue
            if not song.get("bgm"):
                self.stats["played"] += 1
            self.log(f"开始播放《{song['name']}》" +
                     ("（BGM 整首）" if song.get("bgm") else f"（限 {ps:.0f}s）"))
            # 从「真正出声」起算播放时长：mpv 冷启动/取地址的等待
            # 不该占用播放额度
            t0 = time.time()
            song["started_at"] = t0
            # 音量策略：全程恒定 = 音乐音量；说话、切歌都不动音乐音量
            # （想要多大直接在控制台调 music_volume），播放中改也实时生效
            last_lv = self._music_vol()
            killed = False
            next_chk = 0.0
            while True:
                e = time.time() - t0
                if e >= ps or self._skip.is_set():
                    killed = e < ps
                    break
                if song.get("bgm") and time.time() >= next_chk:
                    next_chk = time.time() + 4.0
                    if not self._playing():   # BGM 整首自然播完 → 垫下一首
                        break
                base = self._music_vol()
                if base != last_lv:
                    last_lv = base
                    vq.put(["volume", str(base)])
                time.sleep(0.3)
            vq.put(None)
            self._cli(["stop"], timeout=8)
            self._cli(["volume", str(self._music_vol())], timeout=8)
            with self._lock:
                self._current = None
            if not killed and not song.get("bgm"):
                self.log(f"《{song['name']}》播满 {ps:.0f}s（到时切换）")
            time.sleep(0.5)

    def skip_current(self) -> dict:
        with self._lock:
            cur = self._current
        if cur is None:
            return {"ok": False, "message": "当前没在播歌"}
        self._skip.set()
        return {"ok": True, "message": f"已跳过《{cur['name']}》"}

    def clear_queue(self) -> dict:
        with self._lock:
            n = len(self._queue)
            self._queue.clear()
        self.ensure_worker()             # 覆盖「本来没线程」的情况，让 BGM 接上
        return {"ok": True, "message": f"已清空队列（{n} 首，正在播的歌不受影响）"}

    def test(self, keyword: str) -> dict:
        songs = self.search(keyword or "晴天")
        return {"ok": bool(songs), "count": len(songs), "songs": songs[:5]}
