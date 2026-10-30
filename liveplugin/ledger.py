"""白饭账本：今日进账 / 今日消耗 / 余额。

口径（全部可在控制台里自定义，写进 config.json 的 `ledger` 段）：
- 进账：收到礼物时按「礼物名 → 白饭/个」价目表折算；未列出的礼物按
  「抖币单价 × default_coin_rate」折算（抖币未知时按 default_coin_rate 保底）；
  折算结果再乘「进账倍率 income_multiplier」。
- 消耗：AI 每次调用大模型后，按本次 token 数折算（每百万 token = X 白饭，
  token_cost_per_million），再乘「消耗倍率 spend_multiplier」；也可手动记账。
- 余额 = 初始余额 + 累计进账 − 累计消耗；跨天保留余额，
  「今日进账 / 今日消耗」在本地日期变化时自动清零。
- 状态持久化到 ledger.json（和 config.json 同目录），进程重启不丢。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class RiceLedger:
    def __init__(self, path: str | Path, cfg_getter) -> None:
        self.path = Path(path)
        self._cfg = cfg_getter          # callable -> dict；返回当前 ledger 规则
        self._lock = threading.Lock()
        self._day = _today()
        self._balance = 0.0
        self._income = 0.0              # 今日进账
        self._spend = 0.0               # 今日消耗
        self._gifts = 0                 # 今日收到礼物（份数合计）
        self._tokens = 0                # 今日大模型 token 合计
        self._load()

    # -- 规则 -----------------------------------------------------------------
    def rules(self) -> dict:
        r = dict(self._cfg() or {})
        r.setdefault("currency", "白饭")
        r.setdefault("start_balance", 0.0)
        r.setdefault("income_multiplier", 1.0)
        r.setdefault("spend_multiplier", 1.0)
        r.setdefault("gift_rates", {})
        r.setdefault("default_coin_rate", 1.0)
        r.setdefault("token_cost_per_million", 0.0)
        return r

    @staticmethod
    def _num(v, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # -- 查询 -----------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            self._rollover_locked()
            return self._snap_locked()

    def _snap_locked(self) -> dict:
        r = self.rules()
        return {
            "currency": r.get("currency") or "白饭",
            "balance": round(self._balance, 2),
            "today_income": round(self._income, 2),
            "today_spend": round(self._spend, 2),
            "today_gifts": int(self._gifts),
            "today_tokens": int(self._tokens),
            "day": self._day,
            "rules": {
                "start_balance": self._num(r.get("start_balance")),
                "income_multiplier": self._num(r.get("income_multiplier"), 1.0),
                "spend_multiplier": self._num(r.get("spend_multiplier"), 1.0),
                "gift_rates": dict(r.get("gift_rates") or {}),
                "default_coin_rate": self._num(r.get("default_coin_rate"), 1.0),
                "token_cost_per_million":
                    self._num(r.get("token_cost_per_million")),
            },
        }

    # -- 记账 -----------------------------------------------------------------
    def income(self, amount: float, gift_num: int = 0) -> dict:
        with self._lock:
            self._rollover_locked()
            amount = self._num(amount)
            if amount > 0 or gift_num > 0:
                self._balance += amount
                self._income += amount
                self._gifts += max(0, int(gift_num))
                self._save_locked()
            return self._snap_locked()

    def spend(self, amount: float, tokens: int = 0) -> dict:
        with self._lock:
            self._rollover_locked()
            amount = self._num(amount)
            if amount > 0 or tokens > 0:
                self._balance -= amount
                self._spend += amount
                self._tokens += max(0, int(tokens))
                self._save_locked()
            return self._snap_locked()

    def adjust(self, delta: float) -> dict:
        """手动记账：delta>0 记进账，delta<0 记消耗。"""
        delta = self._num(delta)
        return self.income(delta) if delta > 0 else self.spend(-delta)

    def on_gift(self, gift_name: str, num: int = 1,
                unit_value: float = 0.0) -> dict:
        """礼物进账：优先价目表，其次抖币单价×默认倍率。"""
        r = self.rules()
        rates = r.get("gift_rates") or {}
        rate = rates.get(str(gift_name or "").strip())
        if rate is None:
            unit = self._num(unit_value)
            coin_rate = self._num(r.get("default_coin_rate"), 1.0)
            rate = unit * coin_rate if unit > 0 else coin_rate
        amount = self._num(rate) * max(0, int(num or 0)) \
            * self._num(r.get("income_multiplier"), 1.0)
        return self.income(amount, gift_num=max(0, int(num or 0)))

    def on_tokens(self, prompt_tokens: int, completion_tokens: int) -> dict:
        """大模型 token 消耗折算成白饭。"""
        r = self.rules()
        tokens = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
        amount = (tokens / 1_000_000.0) * self._num(r.get("token_cost_per_million")) \
            * self._num(r.get("spend_multiplier"), 1.0)
        return self.spend(amount, tokens=tokens)

    def reset(self, balance: float | None = None) -> dict:
        """重置账本：余额设为给定值（默认用 start_balance），今日计数清零。"""
        with self._lock:
            r = self.rules()
            self._balance = self._num(
                r.get("start_balance") if balance is None else balance)
            self._income = 0.0
            self._spend = 0.0
            self._gifts = 0
            self._tokens = 0
            self._day = _today()
            self._save_locked()
            return self._snap_locked()

    # -- 持久化 ---------------------------------------------------------------
    def _rollover_locked(self) -> None:
        d = _today()
        if d != self._day:
            self._day = d
            self._income = 0.0
            self._spend = 0.0
            self._gifts = 0
            self._tokens = 0
            self._save_locked()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._balance = round(self._num(
                (self._cfg() or {}).get("start_balance")), 2)
            return
        self._day = str(raw.get("day") or _today())
        self._balance = round(self._num(raw.get("balance")), 2)
        self._income = round(self._num(raw.get("today_income")), 2)
        self._spend = round(self._num(raw.get("today_spend")), 2)
        self._gifts = int(self._num(raw.get("today_gifts")))
        self._tokens = int(self._num(raw.get("today_tokens")))
        if self._day != _today():          # 跨天：今日计数清零，余额保留
            self._day = _today()
            self._income = 0.0
            self._spend = 0.0
            self._gifts = 0
            self._tokens = 0
            self._save_locked()

    def _save_locked(self) -> None:
        data = {"day": self._day, "balance": round(self._balance, 2),
                "today_income": round(self._income, 2),
                "today_spend": round(self._spend, 2),
                "today_gifts": int(self._gifts),
                "today_tokens": int(self._tokens)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:              # 账本持久化失败不影响直播
            print(f"[ledger] 保存失败: {e}")
