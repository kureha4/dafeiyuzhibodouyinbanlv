"""一键取参 / 直通取流：驱动本机 Edge/Chrome 匿名实例打开直播间。

两种用法（都不需要用户手动 F12，只填直播间链接）：
1. auto_fetch_params: 通过 CDP 截获页面发起的 webcast WS 的 URL/Cookie 参数；
2. CdpWssSource: 保持这个匿名浏览器活着，把它页内**已鉴权 WS 连接**的收帧
   直接转交给 wss_source 解码管线——鉴权与心跳由浏览器负责，插件只读数据，
   彻底绕开"重放握手缺 cookie"的问题。

零新增依赖：只需已安装的 Edge/Chrome + websocket-client（标准库 + 现有依赖）。
浏览器用一次性临时匿名 profile（等效无痕），无头、不登录，关闭即整个删除。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import websocket

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for p in _BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def normalize_room_url(s: str) -> str:
    """支持四类输入 → 浏览器可打开的房间页：
    - 地址栏链接 https://live.douyin.com/xxx
    - 分享短链   https://v.douyin.com/xxxx/（浏览器自动跳转）
    - 分享口令文本（里面抠出链接）
    - 纯数字：短号=直播间号走 live.douyin.com；长id走 reflow 中转页
    """
    s = (s or "").strip()
    if s.startswith("http"):
        return s
    m = re.search(r"https?://[A-Za-z0-9./_%?=&-]+", s)   # 分享口令里抠链接
    if m:
        return m.group(0)
    if re.fullmatch(r"\d{4,13}", s):
        return f"https://live.douyin.com/{s}"
    if re.fullmatch(r"\d{14,25}", s):   # 协议内部长id → reflow 页会自动跳转开播
        return f"https://webcast.amemv.com/webcast/reflow/{s}"
    raise ValueError("填直播间链接（地址栏或分享出来的链接）或直播间号即可")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def auto_fetch_params(room_input: str, timeout: float = 35.0,
                      log=lambda m: None) -> dict:
    """返回 {room_id, user_id, device_id, ttwid, signature, cookie, host}。
    先试无头模式（不打扰），失败再开一个可见窗口重试（风控偏好真窗口）。"""
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到 Edge/Chrome（Windows 一般自带 Edge）")
    room_url = normalize_room_url(room_input)
    last_err: Exception | None = None
    for headless in (True, False):
        mode = "无头" if headless else "窗口"
        udir = tempfile.mkdtemp(prefix="liveplugin_wss_")
        port = _free_port()
        args = [browser, "--disable-gpu", "--mute-audio",
                "--disable-blink-features=AutomationControlled",
                f"--user-agent={_UA}", f"--remote-debugging-port={port}",
                f"--user-data-dir={udir}", "--window-size=1920,1080",
                "about:blank"]
        if headless:
            args.insert(1, "--headless=new")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            log(f"{mode}模式取参中（约 10~30 秒，浏览器自动开你的直播间）…")
            ws_page = _devtools_attach(port, timeout)
            try:
                url, cookie, _ws_id = _capture_ws(ws_page, room_url, timeout, log)
            finally:
                try:
                    ws_page.close()
                except Exception:
                    pass
            return _parse_ws_url(url, cookie)
        except RuntimeError as e:
            last_err = e
            log(f"{mode}模式未成功：{e}" + ("，换窗口模式重试…" if headless else ""))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            shutil.rmtree(udir, ignore_errors=True)
    raise RuntimeError(str(last_err) or "取参失败")


def _devtools_attach(port: int, timeout: float):
    """等调试端口就绪，返回首页 target 的 CDP websocket。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                tabs = [t for t in json.load(r) if t.get("type") == "page"]
            if tabs:
                return websocket.create_connection(
                    tabs[0]["webSocketDebuggerUrl"], timeout=10)
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("浏览器调试端口启动失败（可能被杀软拦截）")


def _capture_ws(ws_page, room_url: str, timeout: float, log):
    """先开 Network 监听再导航，截 webSocketCreated 里的完整 WS URL+Cookie。

    Cookie 拿不到握手头的真实值，因此同时挂上 CDP 的 WS 帧流监听
    （browser_ws_id），让 passthrough 模式能直接读浏览器已鉴权连接的收帧。
    """
    def cmd(i, method, **params):
        ws_page.send(json.dumps({"id": i, "method": method,
                                 "params": params}))
    cmd(1, "Network.enable")
    cmd(2, "Page.enable")
    cmd(3, "Page.navigate", url=room_url)
    ws_page.settimeout(1.0)
    t0 = time.time()
    log(f"已打开 {room_url}，等待页面发起 WS 连接…")
    while time.time() - t0 < timeout:
        try:
            msg = json.loads(ws_page.recv())
        except websocket.WebSocketTimeoutException:
            continue
        except Exception:
            break
        m = msg.get("method")
        if m == "Network.webSocketCreated":
            u = (msg.get("params") or {}).get("url", "")
            if "webcast/im/push" in u:
                hdrs = (msg.get("params") or {}).get("headers") or {}
                cookie = next((v for k, v in hdrs.items()
                               if k.lower() == "cookie"), "")
                if not cookie:
                    cookie = _get_document_cookie(ws_page)
                # passthrough 关键：记住浏览器这条 WS 的 requestId
                ws_id = (msg.get("params") or {}).get("requestId", "")
                log("已截获 WS 连接，正在拆参数…")
                return u, cookie, ws_id
    raise RuntimeError(
        f"{timeout:.0f} 秒内没看到 WS 连接——多半是直播间当前没开播，"
        "或页面出现验证码（换「手动粘贴」方式）")


def _get_document_cookie(ws_page) -> str:
    """CDP 读 document.cookie（ttwid/__ac_signature/webid 等页面级 cookie）。"""
    try:
        ws_page.send(json.dumps({"id": 99, "method": "Runtime.evaluate",
                                 "params": {"expression": "document.cookie"}}))
        ws_page.settimeout(5)
        while True:
            m = json.loads(ws_page.recv())
            if m.get("id") == 99:
                return (m.get("result", {}).get("result", {})
                        .get("value", "") or "")
    except Exception:
        return ""


def _parse_ws_url(url: str, cookie: str) -> dict:
    q = urllib.parse.urlparse(url).query
    p = {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}
    out = {
        "ws_url": url,                       # 原始 URL：连接时直接回放最稳
        "room_id": p.get("room_id_str") or p.get("room_id") or "",
        "user_id": p.get("user_id", "0"),
        "device_id": p.get("device_id", ""),
        "ttwid": p.get("ttwid", ""),
        "signature": p.get("signature", ""),
        "cookie": cookie,
        "host": (urllib.parse.urlparse(url).hostname or ""),
    }
    if not out["room_id"]:
        raise RuntimeError("WS URL 里没解析出 room_id")
    return out


# ---------------------------------------------------------------------------
# 直通取流（passthrough）：浏览器自己维持已鉴权 WS 连接，我们只转发收帧。
# 接口刻意对齐 DouyinWssSource（stats / close / _on_open/_on_error/_on_close
# 可被 WssManager 打桩），上层完全无感。
# ---------------------------------------------------------------------------
class CdpWssSource:
    def __init__(self, on_payload, verbose: bool = False, log=None) -> None:
        from .wss_source import DouyinWssSource
        self._log = log or print
        self._inner = DouyinWssSource(on_payload, verbose=verbose,
                                      log=self._log)
        self._inner.on_raw = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._ws_page = None
        self._udir = ""
        self._ws_ids: set[str] = set()       # 页内目标 WS 的 requestId
        self.room_id = ""                    # 从页面 WS URL 里解析出来供 UI 显示
        self._opened = False
        self._thread: threading.Thread | None = None

    @property
    def stats(self) -> dict:
        return self._inner.stats

    # 默认钩子（WssManager 会替换成状态机版本）
    def _on_open(self, ws) -> None: ...
    def _on_error(self, ws, err) -> None: ...
    def _on_close(self, ws, code, msg) -> None: ...

    def connect(self, room_input: str, capture_timeout: float = 45.0) -> None:
        """非阻塞：启动浏览器→导航→后台线程转发帧流。"""
        browser = find_browser()
        if not browser:
            raise RuntimeError("找不到 Edge/Chrome（Windows 一般自带 Edge）")
        room_url = normalize_room_url(room_input)
        self._room_url = room_url
        self._capture_timeout = capture_timeout
        self._thread = threading.Thread(target=self._run, args=(browser,),
                                        daemon=True, name="cdp-wss")
        self._thread.start()

    # -- 后台主循环 ----------------------------------------------------------
    def _run(self, browser: str) -> None:
        for headless in (True, False):           # 无头优先，失败切可见窗口
            if self._stop.is_set():
                return
            self._udir = tempfile.mkdtemp(prefix="liveplugin_cdp_")
            port = _free_port()
            args = [browser, "--disable-gpu", "--mute-audio",
                    "--disable-blink-features=AutomationControlled",
                    f"--user-agent={_UA}", f"--remote-debugging-port={port}",
                    f"--user-data-dir={self._udir}",
                    "--window-size=1280,800", "about:blank"]
            if headless:
                args.insert(1, "--headless=new")
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            try:
                self._log(f"[cdp] {'无头' if headless else '窗口'}模式打开 "
                          f"{self._room_url}")
                self._ws_page = _devtools_attach(port, self._capture_timeout)
                self._pump(self._capture_timeout)
                return                             # 正常结束或用户断开
            except Exception as e:
                if self._stop.is_set():
                    return
                self._log(f"[cdp] {'无头' if headless else '窗口'}模式失败: {e}"
                          + ("，换窗口模式重试…" if headless else ""))
                self._teardown()
        self._on_error(None, "浏览器无法建立直播连接（没开播/验证码）")
        self._on_close(None, None, None)

    def _pump(self, capture_timeout: float) -> None:
        ws = self._ws_page
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Page.enable"}))
        ws.send(json.dumps({"id": 3, "method": "Page.navigate",
                            "params": {"url": self._room_url}}))
        ws.settimeout(1.0)
        t0 = time.time()
        while not self._stop.is_set():
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                if not self._ws_ids and time.time() - t0 > capture_timeout:
                    raise RuntimeError(
                        f"{capture_timeout:.0f} 秒内页面没发起 WS 连接"
                        "（直播间没开播或被风控）")
                continue
            except Exception as e:                 # 浏览器/CDP 断开
                if not self._stop.is_set():
                    self._on_error(None, f"浏览器连接中断: {e!r}")
                break
            method = msg.get("method")
            if method == "Network.webSocketCreated":
                p = msg.get("params") or {}
                u = p.get("url", "")
                if "webcast/im/push" in u:
                    self._ws_ids.add(p.get("requestId", ""))
                    if not self.room_id:
                        q = urllib.parse.parse_qs(
                            urllib.parse.urlparse(u).query)
                        self.room_id = (q.get("room_id_str")
                                        or q.get("room_id") or [""])[0]
                    if not self._opened:
                        self._opened = True
                        self._on_open(None)
            elif method == "Network.webSocketFrameReceived":
                p = msg.get("params") or {}
                if p.get("requestId") not in self._ws_ids:
                    continue
                frame = (p.get("response") or {})
                data = frame.get("payloadData", "")
                if frame.get("opcode") == 2:       # binary
                    try:
                        raw = base64.b64decode(data)
                    except Exception:
                        raw = data.encode("latin-1", "ignore")
                    self._inner.stats["frames"] += 1
                    try:
                        self._inner._emit_binary_frame(raw)
                    except Exception as e:
                        self._inner.stats["errors"] += 1
                        self._log(f"[cdp] 解帧错误: {e!r}")
        self._teardown()
        if not self._stop.is_set():
            self._on_close(None, None, None)

    # -- 拆除 ----------------------------------------------------------------
    def _teardown(self) -> None:
        if self._ws_page:
            try:
                self._ws_page.close()
            except Exception:
                pass
            self._ws_page = None
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None
        if self._udir:
            shutil.rmtree(self._udir, ignore_errors=True)
            self._udir = ""

    def close(self) -> None:
        self._stop.set()
        self._teardown()
