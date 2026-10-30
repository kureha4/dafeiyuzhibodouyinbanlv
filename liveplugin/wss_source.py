"""WSS 逆向数据源：直连抖音 webcast 直播间 WebSocket 获取实时互动数据。

⚠️ 合规提示（务必阅读）：
- 本模块连接的是抖音 **非公开私有协议**（webcast ws），不在开放平台授权范围内，
  使用即构成对平台用户协议的违反，存在账号处罚（警告/限流/封禁）风险。
- 仅建议在你自己的、仅本机使用的调试场景下运行；不要用于商业分发或提审版本。
- 协议字段会随抖音版本更新而失效，需自行维护；代码里把"待你填写的参数"全部集中
  在 connect()，便于换号/换版本时快速改。

与官方方案的关系（架构已解耦，二者可互换）：
- 官方模式：  EventCenter.handle_partner_event("OPEN_LIVE_DATA", payload)   ← 命名管道
- 逆向模式：  DouyinWssSource → 把 webcast 消息归一化成同样的 payload 结构，
              仍然调用 EventCenter.handle_partner_event，下游渲染/AI 零改动。
- 逆向独有收益：有真实 **进场消息 MemberMessage**（官方不开放），欢迎语可复活；
              还能拿到在线人数、进场特效、管理员操作等。

webcast 协议要点（社区通行做法，字段以现网为准）：
- 连接：wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?...
  关键 query：room_id_str、user_id、device_id、app=10013、version_code=180800、
             signature/cookie/ttwid 等鉴权参数（需从浏览器直播间会话抓取）。
- 帧：服务端推送 **protobuf** 二进制包（PushFrame），其 payload 是 gzip 的
  Response；Response.messages[] 每条 (method, payload bytes)，method 常见：
    WebcastMemberMessage      进入直播间（进场！）
    WebcastChatMessage        弹幕/评论
    WebcastGiftMessage        送礼
    WebcastLikeMessage        点赞
    WebcastSocialMessage      关注
    WebcastRoomUserSeqMessage 在线人数
  心跳：客户端定期回 PushFrame 空包（见 _heartbeat）。
- 解析：优先用 protobuf 动态解析（本文件内置最小 wire-format 解码器，零 .proto 依赖，
  能按字段号取出常见文本/整数字段）。若你能提供官方 webcast.proto，
  可替换 parse_push_frame() 用强类型 Message.ParseFromString，更稳。
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import zlib

try:
    import websocket  # websocket-client
except ImportError as e:  # pragma: no cover
    websocket = None
    _WS_IMPORT_ERROR = e


# ---------------------------------------------------------------------------
# 事件类型别名（与 events.MsgType 对齐，避免循环依赖直接引用常量数字）
# ---------------------------------------------------------------------------
T_LIKE, T_COMMENT, T_GIFT, T_FANSC_L, T_FOLLOW = 1, 2, 3, 4, 5

_METHOD_MAP = {
    "WebcastMemberMessage": "member",
    "WebcastChatMessage": "comment",
    "WebcastGiftMessage": "gift",
    "WebcastLikeMessage": "like",
    "WebcastSocialMessage": "follow",
    "WebcastRoomUserSeqMessage": "room_stats",
}


class DouyinWssSource:
    """独立线程维持 WSS 连接，把消息归一化为 OPEN_LIVE_DATA payload 回调出去。

    on_payload(list[dict]) 的每个 dict 结构对齐官方：
        {msg_id, timestamp, msg_type, msg_type_str, sec_open_id, nickname,
         avatar_url, content / gift_num / like_num / user_follow_action}
    """

    def __init__(self, on_payload, on_raw=None, ping_interval: float = 30.0,
                 verbose: bool = False, log=None) -> None:
        self.on_payload = on_payload
        self.on_raw = on_raw
        self.ping_interval = ping_interval
        self.verbose = verbose
        self._log = log or print     # Web 控制台可注入日志回调
        self._ws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stats = {"frames": 0, "messages": 0, "emitted": 0, "errors": 0}
        self._seq = 0
        self._room_id = ""

    # -- 生命周期 ------------------------------------------------------------
    _BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def connect(self, room_id_str: str, *, user_id: str = "0",
                device_id: str = "", cookie: str = "", ttwid: str = "",
                signature: str = "", host: str =
                "webcast5-ws-web-lf.douyin.com", url: str = "") -> None:
        """建立连接。

        url: 传入浏览器截获的**原始 WS URL** 时直接回放（参数与浏览器完全一致，
             一键取参模式走这条路）；否则按参数拼官方模板 URL。
        鉴权参数如何获取见 README『WSS 逆向参数获取』一节。
        """
        if websocket is None:
            raise RuntimeError(f"缺少依赖 websocket-client: {_WS_IMPORT_ERROR}")
        self._room_id = str(room_id_str)
        if url:
            url = url if url.startswith("wss://") else "wss://" + url
        else:
            url = (f"wss://{host}/webcast/im/push/v2/"
                   f"?app=10013&version_code=180800&webcast_sdk_version=1.0.14"
                   f"&room_id_str={room_id_str}&user_id={user_id}"
                   f"&device_id={device_id}&public_params_id=0"
                   f"&signature={signature}&live_id=1&did_rule=3"
                   f"&endpoint=live_pc&support_wrds=1&user_role=anchor"
                   f"&identity=audience&need_official_user=1&ttwid={ttwid}")
        header = {"User-Agent": self._BROWSER_UA,
                  "Origin": "https://live.douyin.com"}
        if cookie:
            header["Cookie"] = cookie
        if ttwid and "Cookie" in header and "ttwid=" not in cookie:
            header["Cookie"] += f"; ttwid={ttwid}"
        elif ttwid and "Cookie" not in header:
            header["Cookie"] = f"ttwid={ttwid}"
        self._stop.clear()
        self._ws = websocket.WebSocketApp(
            url, header=header,
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close)
        self._thread = threading.Thread(target=self._ws.run_forever,
                                        daemon=True, name="wss-source")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # -- WS 回调 -------------------------------------------------------------
    def _on_open(self, ws) -> None:
        if self.verbose:
            self._log("[wss] connected")
        threading.Thread(target=self._heartbeat, args=(ws,), daemon=True).start()

    def _on_message(self, ws, data) -> None:
        self.stats["frames"] += 1
        try:
            if isinstance(data, str):        # 少数情况返回 JSON 文本
                self._emit_text_frame(data)
            else:
                self._emit_binary_frame(bytes(data))
        except Exception as e:
            self.stats["errors"] += 1
            if self.verbose:
                self._log(f"[wss] parse error: {e!r}")

    def _on_error(self, ws, err) -> None:
        self.stats["errors"] += 1
        self._log(f"[wss] error: {err}")

    def _on_close(self, ws, code, msg) -> None:
        if not self._stop.is_set():
            self._log(f"[wss] closed({code}) —— 检查鉴权参数是否过期或协议已变更")

    def _heartbeat(self, ws) -> None:
        """定期回空 PushFrame（服务端要求，否则约 2 分钟被断连）。"""
        while not self._stop.wait(self.ping_interval):
            try:
                ws.send(build_ack_frame(seq=self._seq),
                        opcode=websocket.ABNF.OPCODE_BINARY)
                self._seq += 1
            except Exception:
                break

    # -- 解帧 ----------------------------------------------------------------
    def _emit_binary_frame(self, raw: bytes) -> None:
        from .wss_pb import decode_push_frame, decode_response
        pf = decode_push_frame(raw)
        if self.on_raw:
            self.on_raw("push_frame", pf)
        payload = pf.get("payload", b"")
        # 服务端有时把 gzip 载荷标记成 utf-8（PushFrame.method=compress），
        # 以 gzip 魔数为准最稳。
        if payload and (pf.get("payload_encoding") == "gzip"
                        or payload[:2] == b"\x1f\x8b"):
            try:
                payload = gzip.decompress(payload)
            except (OSError, zlib.error):
                pass
        if not payload:
            return
        resp = decode_response(payload)
        if self.on_raw:
            self.on_raw("response", resp)
        out = []
        for m in resp.get("messages", []):
            conv = self._convert(m.get("method", ""), m.get("payload", b""))
            if conv:
                out.append(conv)
        if out:
            self.stats["emitted"] += len(out)
            self.on_payload(out)

    def _emit_text_frame(self, s: str) -> None:
        try:
            obj = json.loads(s)
        except ValueError:
            return
        if self.on_raw:
            self.on_raw("text", obj)

    # -- 单条消息 → 官方 payload 结构 ------------------------------------------
    def _convert(self, method: str, payload: bytes):
        kind = _METHOD_MAP.get(method)
        if not kind:
            return None
        from .wss_pb import decode_message_fields
        f = decode_message_fields(payload)
        ts = int(time.time() * 1000)
        self._seq += 1
        uid = f.get("user", {}).get("sec_uid", "") or str(f.get("uid", ""))
        nick = f.get("user", {}).get("nickname", "") or "观众"
        avatar = (f.get("user", {}).get("avatar_thumb", {}).get("url_list", [""])[0]
                  if isinstance(f.get("user", {}).get("avatar_thumb"), dict) else "")
        base = {"msg_id": f"wss-{self._seq}", "timestamp": ts,
                "sec_open_id": uid, "nickname": nick, "avatar_url": avatar,
                "user_privilege_level": 0, "is_follow_anchor": False,
                "fansclub_level": 0}
        if kind == "member":           # 进场（官方不开放，逆向独有）
            base.update(msg_type=6, msg_type_str="live_member",
                        member_count=f.get("member_count", 0))
        elif kind == "comment":
            c = f.get("content", "")
            base.update(msg_type=T_COMMENT, msg_type_str="live_comment", content=c)
        elif kind == "gift":
            gid = f.get("gift", {}).get("name", "") if isinstance(
                f.get("gift"), dict) else ""
            base.update(msg_type=T_GIFT, msg_type_str="live_gift",
                        gift_name_plain=gid,
                        gift_num=f.get("total_count", 1) or 1)
        elif kind == "like":
            base.update(msg_type=T_LIKE, msg_type_str="live_like",
                        like_num=f.get("count", 1) or 1)
        elif kind == "follow":
            base.update(msg_type=T_FOLLOW, msg_type_str="live_follow",
                        user_follow_action=1)
        elif kind == "room_stats":
            base.update(msg_type=7, msg_type_str="live_room_stats",
                        online_user_count=f.get("online_user_count", 0))
        return base


# ---------------------------------------------------------------------------
# 心跳包（PushFrame: seq=field1, logid=field2, payload=field8, payloadEncoding=...）
# 最小 wire-format 编码，具体以现网抓包比对（README 说明）。
# ---------------------------------------------------------------------------
def build_ack_frame(seq: int = 0, log_id: int = 0) -> bytes:
    def varint(v: int) -> bytes:
        out = b""
        while True:
            b = v & 0x7F
            v >>= 7
            out += bytes([b | (0x80 if v else 0)])
            if not v:
                return out
    def field(n: int, wt: int) -> bytes:
        return varint((n << 3) | wt)
    # 空 ack：仅带 seq，payload 为 b""
    return field(1, 0) + varint(seq)
