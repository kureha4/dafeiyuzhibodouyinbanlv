"""Web 控制台入口：python -m liveplugin.web

    python -m liveplugin.web                 # http://127.0.0.1:8765
    python -m liveplugin.web --port 9000 --open
    python -m liveplugin.web --wss-room-id 7398xxxx   # 预填并自动连接 WSS
    python -m liveplugin.web --sim                    # 启动即开模拟互动消息

浏览器里即可：连/断 WSS 逆向源、看实时事件流与画布预览、开关模拟器、
手动注入事件、改 AI 人设/节流/密钥、保存 config.json——不用再敲命令行。
"""
from __future__ import annotations

import argparse
import threading
import time
import webbrowser

from .server import WebConsole, serve


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="直播互动插件 Web 控制台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default=None, help="config.json 路径")
    ap.add_argument("--open", action="store_true", help="自动打开浏览器")
    ap.add_argument("--sim", action="store_true", help="启动即开启模拟互动消息")
    ap.add_argument("--wss-room-id", default=None,
                    help="启动即连接该直播间的 WSS 逆向源")
    ap.add_argument("--pipeName", default=None,
                    help="可选：桥接真实直播伴侣管道（一般无需）")
    args = ap.parse_args()

    console = WebConsole(cfg_path=args.config, pipe_name=args.pipeName)
    console.start_app()
    serve(console, args.host, args.port)

    url = f"http://{args.host}:{args.port}"
    print(f"[web] 控制台已启动：{url}   （Ctrl+C 退出）")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if args.sim:
        console.sim_start(1.8)
    if args.wss_room_id:
        room = str(args.wss_room_id)
        saved = console.cfg.get("wss") or {}
        threading.Timer(1.0, lambda: console.wss.connect({
            "room_id": room,
            "user_id": str(saved.get("user_id", "0")),
            "device_id": str(saved.get("device_id", "")),
            "cookie": str(saved.get("cookie", "")),
            "ttwid": str(saved.get("ttwid", "")),
            "signature": str(saved.get("signature", ""))})).start()

    try:
        while not console._quit.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[web] 收到退出信号，正在收尾…")
    finally:
        console._quit.set()
        console.stop()
        print("[web] 已退出")


if __name__ == "__main__":
    main()
