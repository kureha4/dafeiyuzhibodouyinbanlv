"""白饭账本离线测试：礼物进账 / token 消耗 / 倍率 / 跨天清零 / 持久化。

    python tools/test_ledger.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liveplugin.ledger import RiceLedger


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lp_ledger_"))
    path = tmp / "ledger.json"
    rules = {"currency": "白饭", "start_balance": 100,
             "income_multiplier": 1.0, "spend_multiplier": 1.0,
             "gift_rates": {"小心心": 1},
             "default_coin_rate": 1.0, "token_cost_per_million": 1000.0}
    lg = RiceLedger(path, lambda: rules)
    s = lg.snapshot()
    assert s["balance"] == 100 and s["today_income"] == 0, s
    print("① 初始余额 =", s["balance"])

    s = lg.on_gift("小心心", 5)                      # 价目表：1×5
    assert s["today_income"] == 5 and s["balance"] == 105, s
    assert s["today_gifts"] == 5, s
    s = lg.on_gift("玫瑰", 2, unit_value=10)          # 未列入：10×2×系数1
    assert s["today_income"] == 25 and s["balance"] == 125, s
    assert s["today_gifts"] == 7, s
    print("② 礼物进账 =", s["today_income"], "余额 =", s["balance"],
          "礼物份数 =", s["today_gifts"])

    s = lg.on_tokens(500_000, 500_000)               # 1M token × 1000白饭
    assert s["today_spend"] == 1000 and s["balance"] == -875, s
    assert s["today_tokens"] == 1_000_000, s
    print("③ token消耗 =", s["today_spend"], "余额 =", s["balance"],
          "token =", s["today_tokens"])

    s = lg.adjust(+3); s = lg.adjust(-2)
    assert s["balance"] == -874, s
    print("④ 手动记账：余额 =", s["balance"])

    s2 = RiceLedger(path, lambda: rules).snapshot()  # 持久化
    assert s2["balance"] == -874 and s2["today_income"] == 28 \
        and s2["today_spend"] == 1002, s2
    print("⑤ 重启后持久化一致：", s2["balance"], s2["today_income"], s2["today_spend"])

    # 跨天：写昨天日期，重新加载后今日清零、余额保留
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["day"] = "2000-01-01"
    path.write_text(json.dumps(raw), encoding="utf-8")
    s3 = RiceLedger(path, lambda: rules).snapshot()
    assert s3["balance"] == -874 and s3["today_income"] == 0 \
        and s3["today_spend"] == 0, s3
    print("⑥ 跨天清零：余额", s3["balance"], "今日", s3["today_income"], s3["today_spend"])

    s4 = lg.reset(50)
    assert s4["balance"] == 50 and s4["today_income"] == 0 and s4["today_spend"] == 0
    print("⑦ 重置：余额 =", s4["balance"])

    # 倍率生效
    rules2 = dict(rules, income_multiplier=2.0, spend_multiplier=3.0)
    lg2 = RiceLedger(tmp / "l2.json", lambda: rules2)
    lg2.on_gift("小心心", 1)
    s5 = lg2.snapshot()
    assert s5["today_income"] == 2, s5
    lg2.on_tokens(1_000_000, 0)
    s6 = lg2.snapshot()
    assert s6["today_spend"] == 3000, s6
    print("⑧ 倍率生效：进账 ×2，消耗 ×3")

    print("LEDGER TEST OK")


if __name__ == "__main__":
    main()
