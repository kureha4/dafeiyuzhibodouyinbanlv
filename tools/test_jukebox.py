"""点歌队列服务离线测试：假 ncm-cli 验证排队/逐首播放/音量淡出/面板数据。"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liveplugin.jukebox import Jukebox

tmp = Path(tempfile.mkdtemp(prefix="juke_test_"))
calls = tmp / "calls.log"
fake = tmp / "fake_cli.js"
fake.write_text("""
const a = process.argv.slice(2);
const fs = require('fs');
fs.appendFileSync(process.env.JUKE_LOG, a.join(' ') + '\\n');
if (a[0]==='search') {
  const kw = a[a.indexOf('--keyword')+1] || 'x';
  console.log(JSON.stringify({songs:[
    {name:kw, artists:[{name:'周杰伦'}], encryptedId:'E_'+kw,
     originalId:999, visible:true},
    {name:'禁歌', artists:'某', encryptedId:'ENCX', originalId:112,
     visible:false}]}));
  process.exit(0);
}
if (['play','volume','stop'].includes(a[0])) process.exit(0);
if (a[0]==='state') {
  console.log(JSON.stringify({success:true,
    state:{status:'playing', position:1, volume:40}}));
  process.exit(0);
}
if (a[0]==='config') process.exit(0);
process.exit(1);
""", encoding="utf-8")
os.environ["JUKE_LOG"] = str(calls)

cfg = {"enabled": True, "bin": f"node {fake}", "trigger": "点歌",
       "cooldown": 30, "blacklist": ["敏感"], "queue_max": 3,
       "play_seconds": 10, "fade_seconds": 0.5,
       "music_volume": 40, "bgm_enabled": False,
       "voice": "感谢 {nickname} 的点歌：《{song}》"}
spoke = []
jb = Jukebox(lambda: cfg, log=print, on_speak=spoke.append)

# 1) 弹幕触发进队 + visible=false 过滤 + 播报
assert jb.handle_comment("小明", "点歌 晴天") is True
assert jb.handle_comment("小红", "点歌：安静") is True   # 冒号变体
assert jb.handle_comment("小刚", "你好") is False
time.sleep(1.2)
p = jb.panel()
assert p["playing"], p
names = {p["current"]["name"]} | {q["name"] for q in p["queue"]}
assert names == {"晴天", "安静"}, p                     # 两首都进来了
assert len(p["queue"]) == 1, p                         # 一首在播一首排队
assert all(q["name"] != "禁歌" for q in p["queue"]), p  # visible 过滤
assert len(spoke) >= 1 and "点歌" in spoke[0], spoke
print("① 弹幕触发进队/逐首顺序/visible 过滤 OK")

# 2) 队列上限
cfg["queue_max"] = 1  # 当前安静在播/晴天... 队列里还有多少另算：直接测拒绝分支
# 先清队再连点两首：第一首进队，第二首因上限=1被拒（当前无额外排队）
jb.clear_queue()
r1 = jb.request("甲", "咬挖")     # 进队
r2 = jb.request("乙", "后面")     # 队列已有1首 → 拒
assert r1["ok"], r1
assert (not r2["ok"]) and "队满" in r2["message"], r2
cfg["queue_max"] = 3
print("② 队列上限拒绝 OK:", r2["message"])

# 3) 跳过当前 → 下一首开播；play/volume/stop 调用记录
time.sleep(0.8)
p = jb.panel()
assert p["playing"], p
cur_name = p["current"]["name"]
r = jb.skip_current()
assert r["ok"], r
time.sleep(1.5)
p2 = jb.panel()
assert p2["playing"] and p2["current"]["name"] != cur_name, (p, p2)
log = calls.read_text(encoding="utf-8")
assert "play --song --encrypted-id" in log and "volume 40" in log \
    and "stop" in log, log[:400]
print("③ 跳过切歌 + 恒定音量/stop 调用 OK")

# 4) 面板数据结构（渲染器要用）
p3 = jb.panel()
c = p3["current"]
assert {"name", "artist", "nickname", "elapsed", "limit"} <= set(c), c
assert c["limit"] == 10, c
print("④ panel() 字段 OK:", {k: c[k] for k in ('name','nickname','limit')})

# 4.5) 音量恒定：任何时刻不超上限，且不因说话变化（淡入淡出/duck 已移除）
import re
vols = [int(v) for v in re.findall(r"volume (\d+)",
                                   calls.read_text(encoding="utf-8"))]
assert vols and all(v <= 40 for v in vols), vols
assert 0 not in vols, vols        # 不再从 0 淡入
print("④½ 音量恒定 OK:", sorted(set(vols)))

# 5) 冷却与黑名单（同步 request 分支不受 worker 影响）
jb.clear_queue(); jb._queue.clear()
r = jb.request("小明", "随便")
assert not r["ok"] and "冷却" in r["message"], r
r = jb.request("路乙", "敏感歌")
assert not r["ok"] and "黑名单" in r["message"], r
print("⑤ 冷却/黑名单 OK")

# 6) BGM：队列空自动随机垫整首；点歌立刻抢占
cfg["bgm_enabled"] = True
jb.clear_queue(); jb._queue.clear(); jb._skip.set()
jb._ensure_worker()
t0 = time.time()
while time.time() - t0 < 30:
    time.sleep(1)
    st = jb.panel()
    if st["playing"] and st["current"] and st["current"].get("bgm"):
        break
else:
    raise AssertionError("BGM 没垫上")
assert jb.stats["bgm"] >= 1 and jb.panel()["current"]["limit"] == 600
r = jb.request("土豪", "晴天")            # 点歌应打断 BGM
assert r["ok"], r
t0 = time.time()
while time.time() - t0 < 30:
    time.sleep(1)
    st = jb.panel()
    if (st["playing"] and st["current"]
            and not st["current"].get("bgm")
            and st["current"]["name"] == "晴天"):
        break
else:
    raise AssertionError("点歌没抢占 BGM")
assert jb.panel()["current"]["limit"] == 10
cfg["bgm_enabled"] = False
print("⑥ BGM 自动垫歌 + 点歌抢占 OK")

jb.clear_queue()
jb._skip.set()
time.sleep(12)
print("JUKEBOX QUEUE TEST OK")
