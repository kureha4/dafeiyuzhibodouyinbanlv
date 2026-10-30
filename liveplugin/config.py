"""全局配置：从 config.json 加载，缺失字段用内置默认值补齐。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # 运行模式：mock=内存管道端到端自测；native=桥接官方 PipeSDK.dll 接真实伴侣
    "mode": "mock",
    # 布局：0=竖屏(基准宽1080) 1=横屏(基准宽1920)
    "layout_mode": 0,
    "fps": 30,
    # 预览画布 = 最终直播画面（屏幕捕捉这一路即可）。全分辨率合成，所见即所得。
    # layout 里是每一层在画布上的位置/大小（像素），可任意改。
    "canvas": {
        "width": 1080, "height": 1920,      # 竖屏成品；横屏改 1920x1080
        "fps": 24, "quality": 88,           # 预览帧率与 JPEG 质量（捕捉用建议 ≥85）
        "bg": [12, 16, 30],                # 画布纯色背景
        "bg_image": "",                    # 可选：assets/ 下的背景图（铺满画布）
        "layout": {
            "character": {"x": 180, "y": 720, "w": 720, "h": 1080},
            "danmaku":   {"x": 30,  "y": 120, "w": 460, "h": 668},
            "balance":   {"x": 530, "y": 40,  "w": 520, "h": 170},
            "songq":     {"x": 530, "y": 230, "w": 520, "h": 300},
            "bubble":    {"x": 30,  "y": 810, "w": 620, "h": 190},
            "histo":     {"x": 30,  "y": 1020, "w": 460, "h": 140},
        },
    },
    # 素材通道预算（一个插件最多6个通道，单通道≤1920x1080）
    "channels": {
        "character": {"id": 0, "width": 720, "height": 1080},
        "danmaku":   {"id": 1, "width": 620, "height": 900},
        "audio":     {"id": 2},
        "balance":   {"id": 3, "width": 520, "height": 170},
        "songq":     {"id": 4, "width": 520, "height": 300},
        "bubble":    {"id": 5, "width": 620, "height": 190},
        "histo":     {"id": 6, "width": 460, "height": 140},
    },
    # 渲染
    "render": {
        "font_path": "C:/Windows/Fonts/msyh.ttc",
        "font_bold_path": "C:/Windows/Fonts/msyhbd.ttc",
        "character": {
            "name": "小抖",
            "base_hue": 205,        # 角色主色相
            "idle_amplitude": 18.0, # 待机上下浮动像素
            "rotate_seconds": 90,   # 人物视频库随机切换间隔（0=不自动切）
        },
        "danmaku": {
            "bg": [12, 16, 30, 150],       # 半透明面板底色 RGBA
            "max_lines": 12,
            "enter_seconds": 0.35,          # 入场动效时长
            "bubble_colors": {              # 按事件类型高亮
                "comment": [46, 156, 219],
                "gift":    [240, 134, 58],
                "like":    [214, 90, 140],
                "follow":  [87, 185, 138],
                "fansclub":[150, 120, 220],
            },
        },
        # 独立「余额面板」通道（第三路素材，伴侣上可任意拖放）
        "balance": {
            "enabled": True,
            "title": "直播小账本",
            "accent": [255, 214, 90],      # 主色（金）
            "bg": [12, 16, 30, 170],       # 面板底色
            "rice_from": "spend",          # 白饭碗按花费算（改 income 则按收入）
            "font_scale": 1.0,             # 文字大小 ×（0.5~2）
        },
        # 「点歌队列」面板：正在播放+进度条+排队列表
        "songq": {
            "enabled": True,
            "title": "点歌队列",
            "accent": [126, 200, 255],
            "bg": [12, 16, 30, 170],
            "font_scale": 1.0,
        },
        # 「AI 气泡」：大模型回复渲染成对话气泡，自动过期
        "bubble": {
            "enabled": True,
            "bg": [20, 26, 48, 225],         # 气泡底色 RGBA
            "text": [255, 255, 255],         # 文字颜色
            "accent": [126, 200, 255],       # 描边色
            "ttl": 8,                        # 每条显示秒数（3~30）
            "tail": "up",                    # 尾巴方向 up/down/none
            "font_scale": 1.0,               # 文字大小 ×（0.5~2）
        },
        # 「音乐频谱」：随本机出声跳动的直方图（环回采集真实音频，非假跳动）
        "histo": {
            "enabled": True,
            "bars": 28,                      # 柱数 8~64
            "sensitivity": 1.0,              # 灵敏度 0.2~3
            "bg": [12, 16, 30, 150],
            "accent": [126, 200, 255],       # 左端/低柱颜色
            "accent2": [255, 110, 190],      # 右端/高柱颜色
            "peak_cap": True,                # 柱顶余辉小横杆
        },
    },
    # AI 对话（远程 OpenAI 兼容接口；api_key 优先读环境变量 LIVEPLUGIN_API_KEY）
    "ai": {
        "enabled": True,
        "provider": "openai_compatible",
        # 推给 AI 回复的事件类型白名单（进场/关注/点赞等默认不推，自行接 TTS）
        "reply_types": ["comment", "gift"],
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "",                       # 留空则读环境变量
        "persona": "你是一个抖音直播间的 AI 虚拟主播，名字叫{char_name}，性格开朗、口语化、热情，回复简短（不超过40字），不要用markdown。",
        "welcome_template": "欢迎 {nickname} 小伙伴来到直播间～",
        "gift_template": "感谢 {nickname} 送的{gift_name}x{gift_num}！比心～",
        "max_queue": 64,
        "request_timeout": 12.0,
        "max_tokens": 500,                 # 推理型模型要先想再答，预算给够才不空回复
        "temperature": 0.8,
        "max_reply_chars": 60,
        # 事件节流（秒）：同类型事件最短处理间隔，防止刷爆大模型
        "min_interval": {"comment": 2.0, "gift": 1.0, "like": 20.0, "follow": 1.5,
                         "member": 8.0},
        "history_size": 12,                  # 上下文保留条数
    },
    # TTS：本地 GPT-SoVITS api_v2（先起服务：start_api_only.bat，端口 9880）
    "tts": {
        "enabled": True,
        "provider": "gpt_sovits",
        "base_url": "http://127.0.0.1:9880",
        "ref_audio_path": "D:/ProjectFile/声音数据集/wav/001.wav",
        "prompt_text": "前方高能，野兽出现吗，胜利属于我们。",  # 需与参考音频逐字一致
        "prompt_lang": "zh",
        "text_lang": "auto",
        "speed_factor": 1.0,
        "seed": -1,
        "timeout": 120.0,             # 冷启动首条约 18-28 秒，超时务必给足
        "volume": 100,                # 语音音量 0-200（100=原始响度，合成后调增益）
        "local_playback": True,      # 本机即时播放（native 模式同时推伴侣音频通道）
        # 哪些要朗读：reply=AI回复文本；member/follow/like=固定模板语音（不走 AI）
        "speak_types": ["reply", "member", "follow", "like", "song"],
        "templates": {
            "member": "欢迎 {nickname} 进入直播间～",
            "follow": "感谢 {nickname} 的关注～",
            "like": "感谢 {nickname} 的点赞支持～",
        },
    },
    # 点歌中心：弹幕「点歌 歌名」→ 官方 ncm-cli 搜索 → 本机播放
    "jukebox": {
        "enabled": False,
        "bin": "",                       # 默认自动定位 npm 全局安装的 ncm-cli
        "trigger": "点歌",               # 弹幕以此开头（支持 点歌： !点歌 /点歌）
        "cooldown": 60,                  # 同一观众冷却秒数
        "blacklist": [],                 # 黑名单词（歌名含词即拒）
        "queue_max": 8,                  # 队列上限，满了拒绝新点歌
        "play_seconds": 30,              # 每首只播多少秒（默认 30）
        "fade_seconds": 1.5,             # （已废弃：淡入淡出已移除，保留兼容）
        "music_volume": 45,              # 背景音乐音量（恒定，说话不影响音乐）0-100
        "bgm_enabled": True,             # 队列空了自动随机垫歌，保证音乐不停
        "bgm_pool": [],                  # 垫歌曲库（一行一个歌名；空=内置热门池）
        "voice": "感谢 {nickname} 的点歌：《{song}》",
    },
    # 礼物音效：收到礼物播一声（金币等），wav 文件在控制台上传，多个随机挑
    "sfx": {
        "enabled": True,
        "files": [],                     # assets/ 下的 wav 文件名列表
    },
    # 余额（白饭）账本：今日进账/消耗/余额，单位元；1 元 = 1 碗白饭
    "ledger": {
        "currency": "元",
        "start_balance": 0,              # 新账本初始余额
        "income_multiplier": 1.0,        # 进账倍率
        "spend_multiplier": 1.0,         # 消耗倍率
        "gift_rates": {},                # {"礼物名": 白饭/个}
        "default_coin_rate": 1.0,        # 未配置礼物：抖币单价 × 该系数
        "token_cost_per_million": 0.0,   # 每百万 token 消耗多少白饭
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS)))

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        merged = json.loads(json.dumps(DEFAULTS))
        candidates = []
        if path:
            candidates.append(Path(path))
        else:
            here = Path(__file__).resolve().parent.parent
            candidates.append(here / "config.json")
        for c in candidates:
            if c and c.exists():
                with open(c, "r", encoding="utf-8") as f:
                    merged = _deep_merge(merged, json.load(f))
                break
        cfg = cls(merged)
        # 环境变量覆盖密钥（不进版本库）
        env_key = os.environ.get("LIVEPLUGIN_API_KEY")
        if env_key:
            cfg.data["ai"]["api_key"] = env_key
        return cfg

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> dict:
        return self.data
