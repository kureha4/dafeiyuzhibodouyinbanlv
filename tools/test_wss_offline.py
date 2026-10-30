"""WSS 逆向源离线自测：构造 protobuf 报文跑通解帧→归一化→EventCenter。"""
import gzip
import sys
sys.path.insert(0, ".")

from liveplugin.wss_pb import encode_field
from liveplugin.wss_source import DouyinWssSource
from liveplugin.events import EventCenter
from liveplugin.gifts import GiftCatalog


def msg(method: str, body: bytes) -> bytes:
    return encode_field(1, method) + encode_field(2, body)


def user(uid=123, nick="测试观众", sec="sec-abc", avatar="https://avatar/x.jpg") -> bytes:
    img = encode_field(3, avatar)
    return (encode_field(1, uid) + encode_field(3, nick) +
            encode_field(12, img) + encode_field(38, sec))


# --- 构造各业务消息 ---
chat_body = encode_field(5, "主播好") + encode_field(7, user())
gift_body = (encode_field(6, user(uid=9, nick="土豪哥")) +
             encode_field(12, encode_field(1, "小心心") + encode_field(2, 1)) +
             encode_field(22, 3))          # total_count=3
like_body = encode_field(5, 15) + encode_field(8, user(uid=20, nick="点赞狂"))
member_body = encode_field(6, user(uid=30, nick="新进场")) + encode_field(3, 128)
social_body = encode_field(6, user(uid=40, nick="关注君"))

response = (msg("WebcastChatMessage", chat_body) +
            msg("WebcastGiftMessage", gift_body) +
            msg("WebcastLikeMessage", like_body) +
            msg("WebcastMemberMessage", member_body) +
            msg("WebcastSocialMessage", social_body))
# Response.messages 需包进 field1 多次：这里 msg() 已是完整 entry，重新组装
resp_buf = (encode_field(1, msg("WebcastChatMessage", chat_body)) +
            encode_field(1, msg("WebcastGiftMessage", gift_body)) +
            encode_field(1, msg("WebcastLikeMessage", like_body)) +
            encode_field(1, msg("WebcastMemberMessage", member_body)) +
            encode_field(1, msg("WebcastSocialMessage", social_body)))

gz = gzip.compress(resp_buf)
push_frame = (encode_field(1, 7) + encode_field(2, "log-1") +
              encode_field(8, gz) + encode_field(9, "gzip"))

# --- 跑解析 ---
got = []
src = DouyinWssSource(on_payload=lambda items: got.extend(items), verbose=True)
src._emit_binary_frame(push_frame)
print("emitted:", len(got))
for g in got:
    print(" ", g["msg_type_str"], "|", g["nickname"], "|",
          g.get("content") or g.get("gift_name_plain") or g.get("like_num") or
          g.get("user_follow_action") or g.get("member_count"))

assert len(got) == 5
assert got[0]["msg_type_str"] == "live_comment" and got[0]["content"] == "主播好"
assert got[0]["nickname"] == "测试观众" and got[0]["sec_open_id"] == "sec-abc"
assert got[1]["msg_type_str"] == "live_gift" and got[1]["gift_num"] == 3
assert got[2]["msg_type_str"] == "live_like" and got[2]["like_num"] == 15
assert got[3]["msg_type_str"] == "live_member"          # 进场消息！
assert got[4]["msg_type_str"] == "live_follow"

# --- 接到 EventCenter 验证下游零改动 ---
ec = EventCenter(GiftCatalog())
out = []
ec.on_event(out.append)
ec.handle_partner_event("OPEN_LIVE_DATA", {"payload": got})
import time
time.sleep(3.6)
kinds = sorted(e.type for e in out)
print("downstream:", [(e.type, e.nickname, e.gift_name) for e in out])
assert "member" in kinds          # 进场消息已进入事件总线
assert "comment" in kinds and "gift" in kinds and "like" in kinds
print("WSS OFFLINE TEST OK")
