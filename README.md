# 抖音直播伴侣 AI 虚拟主播互动插件

循环播放透明人物动画 + 装修版弹幕墙，实时接收直播间互动数据（评论/礼物/点赞/关注/粉丝团），
推送给 AI 生成回复，并可接本地 GPT-SoVITS 语音克隆（TTS）实时播报。

> 进场（进入直播间）事件官方互动插件体系**不开放**，本期暂不实现；
> 数据接入层已按 msg_type 抽象，官方若开放只需在 `events.py` 增加一类解析。

## 目录结构

```
LiveStreaming/
├── liveplugin/
│   ├── main.py          # 程序入口：解析伴侣启动参数、组装模块、30fps 渲染循环
│   ├── ipc.py           # 管道协议层：Packet 帧封装 + MockPipeClient / NativePipeClient
│   ├── events.py        # 事件中心：OPEN_LIVE_DATA 解析、去重、点赞/礼物连击聚合
│   ├── gifts.py         # 礼物映射：内置官方 8 款 + getOpenGiftPools 全量池合并
│   ├── ai.py            # AI 编排：事件→提示词→远程大模型→回复（模板兜底）+ TTS 占位
│   ├── wss_source.py    # WSS 逆向数据源（webcast 私有协议，含进场消息）
│   ├── wss_pb.py        # 最小 protobuf wire-format 解码器（零 .proto 依赖）
│   ├── web/             # ★ Web 控制台（纯标准库，浏览器里操作全部功能）
│   │   ├── server.py    #   HTTP API + SSE 事件流 + MJPEG 画布预览
│   │   ├── bus.py       #   事件/回复/日志总线
│   │   ├── static/index.html  # 控制台单页 UI
│   │   └── __main__.py  #   入口：python -m liveplugin.web
│   ├── render/
│   │   ├── character.py # 人物动画：外部视频(FFmpeg解码，透明/普通都可) / 程序化卡通兜底
│   │   └── danmaku.py   # 装修弹幕墙：半透明面板、类型色徽章、入场动效、AI 回复气泡
│   └── config.py        # 配置加载（config.json + 环境变量 LIVEPLUGIN_API_KEY）
├── tools/gen_preview.py # 离线生成渲染帧 PNG 预览
├── assets/              # 放 character.webm/mov/mp4/mkv 人物视频素材（可选）
├── config.example.json  # 配置样例（复制为 config.json 修改）
└── preview/             # 生成的预览图
```

## 环境要求

- **Windows 10/11 + Python 3.10+**（开发验证于 3.13）；
- **FFmpeg**：用到外部人物视频素材时需要（把 `ffmpeg`/`ffprobe` 加入 PATH）；
- **AI 大模型**：任意 OpenAI 兼容服务的一个 API Key（不配则只有模板回复，程序照常跑）；
- **可选 · TTS 语音**：需自行部署开源 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)（见下文「语音 TTS」章节，建议 NVIDIA 显卡 ≥6G 显存）；
- **可选 · 点歌**：Node.js（装 ncm-cli 用）+ [mpv](https://mpv.io) 播放器；
- **可选 · WSS 一键取数**：本机装有 Edge 或 Chrome 浏览器即可，无需其它依赖。

## 快速开始（本地自测，无需注册/开播）

```powershell
pip install -r requirements.txt

# 内存管道 + 模拟互动消息 + 本机画布预览，跑 8 秒
python -m liveplugin.main --mock --sim --preview --duration 8

# 离线生成渲染帧预览图
python tools/gen_preview.py
```

## Web 控制台（推荐日常操作方式）

不再需要手敲命令行参数，一条命令起一个浏览器控制台：

```powershell
python -m liveplugin.web               # 打开 http://127.0.0.1:8765
python -m liveplugin.web --open        # 启动同时自动开浏览器
python -m liveplugin.web --sim         # 启动即开启模拟互动消息
python -m liveplugin.web --wss-room-id 7398xxxx   # 启动即连 WSS 逆向源
```

控制台功能（对照原命令行能力）：

| 面板 | 说明 |
|---|---|
| WSS 逆向数据源 | 填 room_id + cookie/ttwid/signature 即可连接/断开；`--wss-room-id`/config 预填；凭据默认只在内存，勾选才写入 config.json |
| 实时事件流 | 弹幕/礼物/点赞/关注/进场/在线人数 + AI 回复，SSE 实时推送，颜色按类型区分 |
| 画布预览 = **最终直播画面** | 全分辨率成品画布（人物 + 弹幕墙 + 余额面板 + 点歌队列 + AI 气泡，按 `canvas.layout` 排位，默认竖屏 1080×1920、24fps、质量88）。点「⛶ 全屏显示」后 OBS 直接**窗口捕捉这个浏览器**即可开播——无需伴侣接收场景 |
| 模拟互动消息 | 等效 `--sim`，间隔可调，等效调试面板「模拟互动消息」按钮 |
| 手动注入事件 | 选类型/填昵称即发一条，等效调试面板手发消息 |
| **弹幕面板装修** | 标题文字/颜色、底部公告轮播、上传透明 PNG 背景图、圆角/描边/透明度/字号、按类型显示开关、文案模板、人物名字/色相——「实时应用」立刻在预览看到效果 |
| **余额面板**（独立画面层） | 六格数据：余额 X 元 / 今日收入 +X / 今日花费 -X / 今日白饭 X 碗（1元=1碗）/ 收到礼物 X 份 / 今日 Token X。第三路素材通道（520×170），在画布上的位置由 `canvas.layout.balance` 调；装修卡片可开关与改标题 |
| **点歌中心 + 队列面板**（独立画面层） | 弹幕「点歌 歌名」→ 排队 → 逐首播放（每首仅 30 秒、音量恒定）；冷却/黑名单/队列上限/手动点歌/跳过/清队都在卡片里；画面上第四路「点歌队列」面板显示当前歌+进度条+排队列表（`canvas.layout.songq` 调位置） |
| **自动 BGM（音乐不停）** | 队列空了自动从曲库随机垫一首整首播，保证直播间一直有背景音乐；来点歌立刻让位给观众点的歌。开关 + 自定义「垫歌曲库」（留空=内置热门华语池）在点歌卡片里；开播即生效无需点歌 |
| **礼物音效** | 收到礼物播一声短音效（金币/叮咚）。在装修卡片「礼物音效」上传 `.wav`（多个会随机挑着播，上传当场试听），可开关；没有音效则不出声 |
| **人物视频随机切换** | 人物视频库里放 ≥2 条视频时，每隔 `rotate_seconds`（默认 90，可改，0=不切）自动随机换一条，从该视频第一帧重播；也可在「当前使用」下拉手动切 |
| **音乐频谱（跳动的直方图）** | 画布上再加一层随音乐跳动的柱状频谱。用 Windows 环回录制抓**本机扬声器真正发出的声音**做 FFT——BGM、点歌、语音、音效都会驱动它，完全静音时柱子落回 0（不是假跳动）。装修卡片可调柱数/灵敏度/颜色/余辉小杆，位置大小在「屏幕布局」里拖 |
| **AI 气泡**（独立画面层） | 大模型的每句回复自动渲染成**对话气泡**（圆角框+小尾巴，自动换行/省略号，进出场淡入淡出），显示在画布上（默认在人物下方，尾巴朝上指向角色），默认显示 8 秒自动消失。第五路素材通道（620×190），位置大小在「屏幕布局」调（`canvas.layout.bubble`），尾巴方向/停留秒数/字号/开关也在布局卡片里点「应用布局与样式」 |
| AI 设置 | 人设/模板/模型/密钥/节流/回复token预算实时热更新，无需重启；保存到 config.json。**推送白名单 `reply_types`**：默认只把「弹幕评论+送礼」推给 AI 回复（弹幕和评论是同一个东西，都走这一路；送礼也由大模型生成感谢语）。**以「点歌」开头的弹幕由点歌台接管，不推给 AI**（AI 不会再回「我不会唱这首歌」拆台）。回复完全异步：模型慢一点也照播不丢；失败只在日志提示，不再往弹幕墙发「收到xx的弹幕」这类假模板。推理型模型需 `max_tokens≥400`（要先想再答，太小会空回复）；进场/关注/点赞默认不推 AI——仍进弹幕墙与事件流，由 TTS 模板语音播报 |
| **余额账本** | 单位「元」。礼物自动进账（自定义品类价目：`礼物名=元/个`，未列出的按抖币单价×系数，再乘进账倍率）；AI 大模型每次调用按 token 自动记消耗（每百万 token = X 元 × 消耗倍率），并统计今日礼物份数与 token 总数；支持手动记账与一键重置。余额跨天保留、今日计数每天 0 点清零；状态存 `ledger.json` 重启不丢 |
| 运行统计/日志 | 帧数、事件数、去重数、WSS 收发/错误计数 + 全量运行日志 |

冒烟测试（控制台跑起来后另开终端）：`python tools/test_web_http.py`

### 屏幕捕捉开播（成品画布 = 最终直播画面）

直播伴侣不需要接收任何场景：控制台中间的画布就是**最终成品**（全分辨率
1080×1920，默认布局：人物底部居中、弹幕墙左上、余额面板右上）。

1. 点画布下方「⛶ 全屏显示」——整个屏幕就是直播画面；
2. OBS「窗口捕捉」（或显示器捕捉）抓这个浏览器窗口；
3. 语音（TTS 合成）在本机播放，OBS 采集「系统声音」即可带上；
4. 要换布局/分辨率：装修卡片里选横竖屏「实时应用」，或直接在
   `config.json` 的 `canvas.layout` 里改每层 `{x,y,w,h}`（改完点实时应用）。

### 人物素材：透明视频和普通视频都能用

「人物参数」卡片里直接上传视频（mp4/webm/mov/mkv，≤48MB），上传即加入**人物视频库**
（`assets/character.*`、`character2.*`…）并立即启用；「当前使用」下拉随时切换，
选中即自动生效；「删除所选」删库内视频，删的是正在用的会自动切到下一个。
短视频会预解码进内存循环播放，长视频流式解码、循环点无空窗（不会闪出默认卡通）。

- **透明视频**（webm/mov 带 Alpha，如 AE 导出的带透明通道素材）→ 拖进来直接用；
- **普通视频**（手机拍的 mp4、下载的视频）→ 两种用法：
  1. 不抠像：整幅不透明显示（适合当整屏视频素材）；
  2. **开「背景抠像」**：勾选后选背景色（绿幕选绿色、白墙选白色），
     容差/边缘柔化可调，背景会被抠成透明——效果等同透明素材，
     适合"绿幕前拍/录的真人视频"直接当虚拟形象用。

原理：本机 FFmpeg 逐帧解码为 RGBA，再按容差把接近背景色的像素 alpha 置 0
（边缘按柔化参数线性过渡），因此普通视频也能进人物通道/OBS 叠加。
需要本机安装 FFmpeg（`ffmpeg`/`ffprobe` 在 PATH；上传时会用它校验视频）。

## WSS 逆向数据源（只填直播间链接）

控制台 WSS 卡片顶部有「**只填直播间链接（推荐，全自动）**」框：粘贴以下任意一种，
点「一键取数」即可，无需 F12、无需填任何凭据：

- 地址栏链接 `https://live.douyin.com/<直播间号>`
- App 分享出来的短链 `https://v.douyin.com/xxxx/`（自动跟随跳转）
- 分享口令文本（自动抠出链接）
- 直播间号（短号或 19 位内部 id 均可）

**原理（与市面同类工具一致）**：`signature` 由抖音前端 JS 生成，无法离线伪造
（实测匿名直连返回 `auth failed`）。程序改为驱动一个**本机一次性匿名浏览器实例**
（无头优先、失败切可见窗口；全新临时 profile，等效无痕，用完即删）打开直播间，
用 CDP 监听它页内**已鉴权 WebSocket 连接**的下行帧，直接转交解码管线
（`liveplugin/wss_auto.py::CdpWssSource`）——鉴权与心跳由浏览器负责，插件只读数据，
因此不存在「重放缺 Cookie」问题，也不会带出你的账号凭据。

> 已用真实热播间端到端验收：评论/进场/点赞/人气全部解出真实昵称与正文。

**备用：手动填鉴权参数**（浏览器 F12 抓 `webcast/im/push` 那条 WS 的 URL 粘进
「懒人模式」→自动解析）；ttwid 留空时程序自动匿名申领。凭据默认只在内存，
勾选「凭据保存」才写入 config.json。若直播间当前未开播（enter 接口 `status=4`），
连接只会收到心跳、无弹幕，属正常现象。

> ⚠️ 私有协议、非开放平台授权范围，账号有处罚风险；详见 `wss_source.py` 顶部合规提示。

## 语音 TTS（本地 GPT-SoVITS，clone 音色）

本插件只是**调用方**；语音引擎 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 是独立的开源项目（MIT 协议），需要你自己先在本机跑起来：

1. 到 GPT-SoVITS 官方仓库 README 下载 **Windows 整合包**（内含 Python 环境与预训练模型，解压即用；国内可从其语雀/魔搭镜像链接加速下载）。
2. 解压到**纯英文、无空格路径**（如 `D:\GPT-SoVITS`），显存建议 ≥6G。
3. 启动 API 服务：在整合包目录打开终端执行 `runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml`，终端显示 `Uvicorn running on http://127.0.0.1:9880` 即就绪（本插件默认对接 `api_v2`，端口 9880）。
4. 准备一段 **3~15 秒的干净人声 wav** 作为参考音频（cloning 目标音色），并记下这段话的逐字文本。
5. 打开 Web 控制台「语音 TTS」卡片：填参考音频路径 + 逐字一致的参考文本 → 「应用并保存」→「试听」（**首次冷启动约 20-30 秒**，稳态 3-8 秒）。
6. 播报范围可勾选：AI 回复（大模型文本）/ 进场 / 关注 / 点赞（后三者用固定模板，不走 AI，见 `tts.templates`）；合成在本机即时播放，native 模式同时转 48k 立体声 PCM 推伴侣音频通道。
7. 合成走队列串行（GPT-SoVITS 内部就是全局锁），绝不阻塞弹幕与 AI 回复；失败只记日志跳过。
8. 换音色 = 换参考音频路径；换微调模型仍用 GPT-SoVITS 自己的 `/set_gpt_weights` 等接口。

> 注意：GPT-SoVITS 整合包体积数 GB，请勿把引擎目录提交进本仓库；本仓库只包含接入代码 `liveplugin/tts.py`。

## 点歌（弹幕点歌 → 排队逐首播放 → 官方 ncm-cli）

一次性准备（不使用点歌功能可整节跳过）：
1. 装官方 CLI：`npm install -g @music163/ncm-cli`（需先安装 [Node.js](https://nodejs.org)），播放用 [mpv](https://mpv.io/installation/) 并加入 PATH；
2. 配置密钥：在网易云开放平台「账户信息」页申请 appId/privateKey，控制台「点歌中心」卡片里填入；
3. 登录一次：控制台「点歌中心」→ 点「**扫码登录**」→ 用网易云 App 扫码确认。

开播用法：观众弹幕发 `点歌 晴天` → 自动搜索第一首可播版本 → **进入队列** →
按顺序逐首播放，**每首只播 30 秒**（可改 10~120 秒），音量**全程恒定**
（淡入淡出已按需求移除；主播说话完全不影响音乐，两边音量在卡片里手动调）
→ TTS 播报「感谢 XX 的点歌：《晴天》」。

画面上有独立「**点歌队列**」面板（第四路素材，位置大小在「屏幕布局」里调）：
正在播放 + 进度条 + 点播人 + 排队列表。卡片里还能：每人冷却、黑名单词、队列
上限（默认 8 首，满了拒绝）、手动点歌、跳过当前、清空队列、开关面板。

规则细节：搜索结果里 `visible=false`（无版权）的歌自动跳过；触发词支持
`点歌 歌名`、`点歌：歌名`、`!点歌 歌名`、`/点歌 歌名`。

**无声排查（重要）**：ncm-cli 0.1.7 新加播放器 daemon 在慢机器上 3 秒超时无响应，
`play` 会表面成功实际不出声。插件已对每次 CLI 调用强制 `NCM_LEGACY_PLAY=1`
（旧版直连播放，实测正常），并在每次 play 后用 `state` 核实 status=playing——
播放器没真出声会记日志「播放器未真正出声」并口头提示观众。另外**服务不要在受限/
沙箱环境里启动**（如被自动化工具带起），双击 `tools\run_server.cmd` 是最稳的；
一键重启双击 `tools\restart_server.cmd`。

## 接入真实直播伴侣

1. 复制 `config.example.json` 为 `config.json`，按需改人设、样式、AI 参数；
   API 密钥建议放环境变量 `LIVEPLUGIN_API_KEY`。
2. 下载官方**调试版直播伴侣**（见开放平台《互动工具开发者指南（仅直播伴侣）》），
   打开顶部「互动插件调试面板」。
3. 启动方式二选一：
   - **直接调试**：调试面板「启动文件」选 python 启动脚本（伴侣会带
     `--pipeName=xxx --maxChannels=6 --mateVersion=... --layoutMode=...` 参数拉起，
     程序检测到 pipeName 自动进入 native 模式连命名管道）；
   - **打包 exe**：`pyinstaller --onefile -n plugin main.py` 的等价物（入口建议加一个
     根目录 `run_plugin.py`），产物选到调试面板。
4. 调试面板点「模拟互动消息」即可验证全链路；正式接管道时 `ipc.NativePipeClient`
   按 `pack_frame` 字节流读写 `\\.\pipe\{pipeName}`。
   ⚠️ 若提审要求严格走官方 PipeSDK.dll（C++），两种落地方式：
   官方 C++ Demo 做壳 + 本机转发给本 Python 进程；或 ctypes 直调 PipeSDK.dll。
   上层业务模块零改动。
5. 正式上线路径：抖音开放平台 → 创建「直播小玩法 → 互动插件(exe 型)」→ 申请
   评论/礼物/点赞/粉丝团/关注 开放数据能力 → 上传包体提审 → 发布。
   **产物类型（exe）审核通过后不可变更**，提前确认。

## 配置要点

| 键 | 说明 |
|---|---|
| `mode` | `mock` 内存自测 / `native` 连真实伴侣（带 `--pipeName` 时自动 native） |
| `channels.*` | 素材通道 id 与分辨率（单通道 ≤1920x1080，一个插件 ≤6 通道） |
| `render.character` | 人物名/主色相/待机浮动；`rotate_seconds` 人物视频库随机切换间隔（默认 90，0=不自动切，需库里有 ≥2 条视频）；`assets/character.*` 存在时优先用外部视频（透明视频直接可用；普通视频可开 `chroma` 背景抠像变成透明） |
| `jukebox` | 点歌中心：`enabled` 开关、`trigger` 触发词（默认「点歌」）、`cooldown` 每人冷却秒、`blacklist` 黑名单词、`queue_max` 队列上限、`play_seconds` 每首秒数（默认 30）、`music_volume` 音乐音量 0-100（默认 45，全程恒定，说话不影响音乐；`tts.volume` 单独调语音响度 0-200）、`bgm_enabled` 队列空自动垫歌（默认开）、`bgm_pool` 垫歌曲库（一行一个歌名，空=内置池）；密钥与登录态存在 ncm-cli 自己的加密配置里 |
| `sfx` | 礼物音效：`enabled` 开关、`files` 音效 wav 文件名列表（存 assets/，多个随机播）。控制台「装修 → 礼物音效」上传/开关/试听 |
| `render.histo` | 音乐频谱：`enabled`、`bars` 柱数 8~64、`sensitivity` 灵敏度 0.2~3、`accent`/`accent2` 两端颜色、`peak_cap` 柱顶余辉小杆。需要 `pip install numpy pyaudiowpatch`（已装）。关闭时录音线程自动停止不占麦克风。⚠️ 频谱用通道 id 6——Web 画布模式无限制；将来若走官方伴侣（≤6 通道），需砍掉或合并一路（如气泡并入弹幕墙） |
| `ledger` | 余额账本（单位元）：`currency`、`start_balance` 初始余额、`income_multiplier` 进账倍率、`spend_multiplier` 消耗倍率、`gift_rates` 礼物价目（礼物名→元/个）、`default_coin_rate` 未列礼物抖币折算系数、`token_cost_per_million` 每百万 token 的元成本；同时统计今日礼物份数/token 总数；状态存 `ledger.json`（可用环境变量 `LIVEPLUGIN_LEDGER` 改路径） |
| `canvas` | **最终画面画布**（屏幕捕捉这一路就是直播内容）：`width/height`（默认 1080×1920 竖屏）、`fps` 合成帧率、`quality` JPEG 质量（捕捉建议 ≥85）、`bg` 纯色背景或 `bg_image`（assets/ 下图片铺满）、`layout` 每层位置大小 `{character/danmaku/balance/songq: {x,y,w,h}}` |
| `render.balance` / `render.songq` | 余额面板 / 点歌队列面板：开关、标题、颜色、圆角描边；`font_scale` 文字大小倍率（0.5~2，觉得挤就调小）。控制台点「实时应用 / 应用布局与样式」即自动保存到 config.json |
| `tts` | 本地 GPT-SoVITS：`base_url`、`ref_audio_path`（3-15 秒参考音频）、`prompt_text`（逐字一致）、`speed_factor`、`speak_types`（reply/member/follow/like 哪些朗读）、`templates`（进场/关注/点赞播报模板）、`timeout`（默认 120s，冷启动慢） |
| `ai.base_url/model/api_key` | 任意 OpenAI 兼容服务；密钥留空读环境变量 |
| `ai.min_interval` | 各事件类型进 AI 的最短间隔（防刷爆） |
| `tts.enabled` | TTS 总开关；需要先按「语音 TTS」章节把本机 GPT-SoVITS api_v2 服务跑起来 |

## 已验证

- 帧协议 pack/unpack 往返、RGBA 透明通道渲染（preview/*.png）
- 事件去重（msg_id）、点赞 2s 聚合、礼物连击 3s 合并、礼物名映射
- Mock 管道全流程（createMediaSources / subscribeEvent / getOpenGiftPools 响应）
- AI 模板兜底与远程调用降级；主程序 30fps 循环、`--duration` 自限退出
- WSS 离线解析（tools/test_wss_offline.py：PushFrame→gzip→消息→进场/弹幕/礼物/点赞/关注）
- Web 控制台全流程（tools/test_web_http.py：注入5类事件→事件流→AI回复→热更新→预览JPEG）
- 弹幕装修层（tools/test_render_deco.py + tools/test_deco_http.py：标题/公告轮播/背景图/模板/开关）
