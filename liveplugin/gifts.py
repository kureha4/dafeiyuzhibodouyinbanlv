"""礼物映射：sec_gift_id → 礼物名/图标/价格。

两个来源，优先级从高到低：
1. 运行时 getOpenGiftPools（伴侣 >= 8.0.0，礼物 40+ 款，权威）
2. 官方文档内置的 8 款固定对照表（旧伴侣兜底）
"""
from __future__ import annotations

# 官方文档《互动工具开发者指南（仅直播伴侣）》给出的固定 8 款礼物对照
_DEFAULT_POOL = [
    {"gift_name": "小心心", "gift_value": 1,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/154c9946db73cba98529ab66792e5385.png",
     "sec_gift_id": "VQCVfwVeQCJZDCr/srE99db/NA3vgkBlaxG2dPyetE8NLgDRQ5ktGcwXm8M="},
    {"gift_name": "抖音音符", "gift_value": 1,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/802a21ae29f9fae5abe3693de9f874bd",
     "sec_gift_id": "4R89bzhTU5UMh+fbM087VVM47+n8O6kV5mfoDZABs/qktwZSG9TOsEyX8qc="},
    {"gift_name": "人气票", "gift_value": 1,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/e9b7db267d0501b8963d8000c091e123.png",
     "sec_gift_id": "ScShUKZLl04ky+mv/rvpfY/KR79abl/xF82G6WoP/lpN34gTBhWbl7Ag6jI="},
    {"gift_name": "玫瑰", "gift_value": 1,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/4ebf31ca09f2f3d80895bf034fa79d14.png",
     "sec_gift_id": "pXPMpuOKRnPKYBISe95O0gISMwPdCi4L5BqnezAmRsaZnP3WLKRtsoKk0Cg="},
    {"gift_name": "大啤酒", "gift_value": 2,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/a24b3cc863742fd4bc3de0f53dac4487.png",
     "sec_gift_id": "f2STGmVCVFRZUmPKXD/dKUdO3bRJlFKnnyK19C0KoRbE/MOx+yBMqbArtlU="},
    {"gift_name": "你真好看", "gift_value": 2,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/4960c39f645d524beda5d50dc372510e.png",
     "sec_gift_id": "peF61t4DgAzDtYJohdRKi7xgW1ZTHtOsfWVAmFk5GcZ7FzshBI+qObDgOos="},
    {"gift_name": "Thuglife", "gift_value": 99,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/eee04e798ad7f08c9faf577f52e258f9.png",
     "sec_gift_id": "RBNKbYDuBwDIIrHalV8qCPbvt2Jr3p2iVFHvKOM2fWfCZm3mY6Ut9HONRqs="},
    {"gift_name": "亲吻", "gift_value": 99,
     "gift_icon": "https://sf1-cdn-tos.huoshanstatic.com/obj/webcast/7e5ffdd6b8ba48e2f4f5b79fb9c1259c.png",
     "sec_gift_id": "v0WMOIdAoC4r17IR24HWYueiXMPlDDA/qi7G1nFl2IrGg+jah93zKLpfljg="},
]


def default_gift_pool() -> list[dict]:
    return [dict(g) for g in _DEFAULT_POOL]


class GiftCatalog:
    """礼物字典。先挂内置 8 款，连上伴侣后 merge getOpenGiftPools 的全量池。"""

    def __init__(self) -> None:
        self._by_id: dict[str, dict] = {}
        for g in _DEFAULT_POOL:
            self._by_id[g["sec_gift_id"]] = g

    def merge_pool(self, gift_pool: list[dict]) -> int:
        n = 0
        for g in gift_pool or []:
            gid = g.get("sec_gift_id")
            if gid and gid not in self._by_id:
                self._by_id[gid] = g
                n += 1
        return n

    def lookup(self, sec_gift_id: str) -> dict | None:
        return self._by_id.get(sec_gift_id)

    def name_of(self, sec_gift_id: str) -> str:
        g = self._by_id.get(sec_gift_id)
        return g["gift_name"] if g else "神秘礼物"

    def icon_of(self, sec_gift_id: str) -> str:
        g = self._by_id.get(sec_gift_id)
        return g.get("gift_icon", "") if g else ""

    def value_of(self, sec_gift_id: str) -> int:
        g = self._by_id.get(sec_gift_id)
        return int(g.get("gift_value", 0)) if g else 0

    def __len__(self) -> int:
        return len(self._by_id)
