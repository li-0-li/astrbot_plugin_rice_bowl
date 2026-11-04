# -*- coding: utf-8 -*-
"""我的饭碗：把 DeepSeek 账户余额换算成一份一份的东西，并记进账/消费流水。

一份多少钱、叫什么、量词是什么，全部可配。默认一碗白米饭 = 0.5 元，
改成"杯奶茶 12 元""颗糖 0.2 元"都行——余额除以单价就是还剩几份。
每次查到余额有变化就落一行流水，所以她能说清「什么时候多了几份、什么时候少了几份」。

原先跑在 private_companion 的 rice_bowl.py 里（whale-patch-20261024-rice /
whale-patch-20261026-ledger），那个插件 11-02 被整体禁用后单独拆出来。
与原来最大的不同：所有网络请求都扔进线程池，不阻塞 AstrBot 的事件循环。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

PLUGIN_NAME = "astrbot_plugin_rice_bowl"
_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")
_DEFAULT_BASE = "https://api.deepseek.com"
_BALANCE_PATH = "/user/balance"
_TZ = timezone(timedelta(hours=8))

_data_dir = Path(__file__).resolve().parents[2]      # core/data
_plugin_data = _data_dir / "plugin_data" / PLUGIN_NAME
_LEDGER = _plugin_data / "rice_ledger.jsonl"
_STATE = _plugin_data / "rice_ledger_state.json"

_lock = threading.RLock()
_cache: dict = {"at": 0.0, "value": None, "key": "", "base": ""}
_MIN_DELTA = 0.01        # 元，低于一分钱的抖动不算一笔
_START_DELAY = 45.0      # 首次采样延迟，别和启动抢资源


def _log(msg: str) -> None:
    try:
        logger.info("[rice_bowl] %s", msg)
    except Exception:  # noqa: BLE001
        pass


def _iso(ts: float | None = None) -> str:
    try:
        return datetime.fromtimestamp(ts if ts else time.time(), _TZ).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 余额 ----------------

_VERSION_SUFFIX = ("/v1", "/api/v1", "/v1/openai", "/openai/v1")


def _balance_url(base: str) -> str:
    """把填进来的 base 拼成余额接口地址。

    容错三种常见填法：带尾斜杠、顺手填了 /v1、已经写全了 /user/balance。
    """
    b = (base or "").strip().rstrip("/") or _DEFAULT_BASE
    if b.endswith(_BALANCE_PATH):
        return b
    low = b.lower()
    for s in _VERSION_SUFFIX:
        if low.endswith(s):
            b = b[: -len(s)]
            break
    return b.rstrip("/") + _BALANCE_PATH


def _root_of(base: str) -> str:
    """从 https://api.deepseek.com/v1 里剥出 https://api.deepseek.com。"""
    try:
        sp = urlsplit((base or "").strip())
        if sp.scheme and sp.netloc:
            return "%s://%s" % (sp.scheme, sp.netloc)
    except Exception:  # noqa: BLE001
        pass
    return ""


def mask_key(key: str) -> str:
    """日志里只留头尾，不整串打出来。"""
    k = (key or "").strip()
    if len(k) <= 12:
        return "***" if k else ""
    return k[:6] + "..." + k[-4:]


def _key_candidates() -> list[str]:
    """兜底：从 cmd_config.json 里捞 sk- 开头的 key。

    只在既没手填、又没从 provider 探到的时候才用得上，留给老版本 AstrBot。
    """
    out: list[str] = []
    for p in (_data_dir / "cmd_config.json", _data_dir.parent / "cmd_config.json"):
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for m in _KEY_RE.findall(txt):
            if m not in out:
                out.append(m)
    return out


def _provider_keys(p) -> list[str]:
    keys = getattr(p, "api_keys", None)
    if not keys:
        cfg = getattr(p, "provider_config", None) or {}
        keys = cfg.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    return [str(k).strip() for k in (keys or []) if str(k).strip().startswith("sk-")]


def _provider_base(p) -> str:
    client = getattr(p, "client", None)
    bu = getattr(client, "base_url", None) if client is not None else None
    if bu is not None:
        return str(bu)
    cfg = getattr(p, "provider_config", None) or {}
    return str(cfg.get("api_base") or cfg.get("base_url") or "")


def auto_credential(context) -> tuple[str, str]:
    """从 AstrBot 已经配好的供应商里猜一个能查余额的 key。

    优先挑 base_url 里带 deepseek 的；都不是 deepseek 就退回第一个有 key 的。
    所以大部分人装完不用填任何东西 —— 只要「模型提供商」里已经配过 DeepSeek。
    """
    try:
        provs = list(context.get_all_providers() or [])
    except Exception:  # noqa: BLE001
        provs = []
    fallback: tuple[str, str] = ("", "")
    for p in provs:
        keys = _provider_keys(p)
        if not keys:
            continue
        base = _provider_base(p)
        if "deepseek" in base.lower():
            return keys[0], (_root_of(base) or _DEFAULT_BASE)
        if not fallback[0]:
            fallback = (keys[0], (_root_of(base) or _DEFAULT_BASE))
    return fallback


def fetch_balance_sync(api_key: str, api_base: str = _DEFAULT_BASE,
                       timeout: float = 12.0, *, use_cache: bool = True) -> dict:
    """返回 {"ok":bool, "cny":float, "raw":str, "error":str}。永不抛。"""
    api_base = (api_base or _DEFAULT_BASE).strip() or _DEFAULT_BASE
    now = time.time()
    if (
        use_cache
        and _cache["value"] is not None
        and _cache["key"] == api_key
        and _cache["base"] == api_base
        and now - _cache["at"] < 60.0
    ):
        return _cache["value"]
    if not api_key:
        return {"ok": False, "cny": 0.0, "raw": "", "error": "no_api_key"}
    req = urllib.request.Request(
        _balance_url(api_base),
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "cny": 0.0, "raw": "",
                "error": "http_%s" % (getattr(e, "code", None) or "?")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "cny": 0.0, "raw": "", "error": repr(e)}
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"ok": False, "cny": 0.0, "raw": body[:200], "error": "bad_json"}
    cny = 0.0
    for info in (data.get("balance_infos") or []):
        if str(info.get("currency", "")).upper() == "CNY":
            try:
                cny = float(info.get("total_balance") or 0)
            except Exception:  # noqa: BLE001
                cny = 0.0
            break
    got = {"ok": True, "cny": round(cny, 2), "raw": body, "error": ""}
    _cache["at"], _cache["value"] = now, got
    _cache["key"], _cache["base"] = api_key, api_base
    return got


async def fetch_balance(api_key: str, api_base: str = _DEFAULT_BASE,
                        timeout: float = 12.0, *, use_cache: bool = True) -> dict:
    """异步版：请求在线程池里跑，事件循环不等网络。"""
    return await asyncio.to_thread(
        fetch_balance_sync, api_key, api_base, timeout, use_cache=use_cache
    )


# ---------------- 计量单位 ----------------

_UNIT_DEFAULTS = {"measure": "碗", "name": "白米饭", "price": 0.5}


def unit_of(measure: str = "", name: str = "", price: float = 0.5) -> dict:
    """把配置里的三个字段规整成一个单位描述。

    measure 是量词（碗/杯/颗/份），name 是名称（白米饭/奶茶/糖），
    price 是一份多少钱。缺的用默认值补，非法值也退回默认。
    """
    m = str(measure or "").strip() or _UNIT_DEFAULTS["measure"]
    n = str(name or "").strip() or _UNIT_DEFAULTS["name"]
    try:
        p = float(price)
    except Exception:  # noqa: BLE001
        p = _UNIT_DEFAULTS["price"]
    if p <= 0:
        p = _UNIT_DEFAULTS["price"]
    return {"measure": m, "name": n, "price": p}


def unit_label(unit: dict | None = None, with_name: bool = True) -> str:
    """拼出「碗白米饭」这种整体称呼；with_name=False 时只给量词「碗」。"""
    u = unit or unit_of()
    if with_name and u.get("name"):
        return "%s%s" % (u["measure"], u["name"])
    return u["measure"]


def _div_money(cny: float, price: float) -> Decimal:
    """元 ÷ 单价，用 Decimal 精确算，避免二进制浮点的尾差。"""
    money = Decimal(str(round(max(0.0, float(cny or 0)), 2)))
    p = Decimal(str(price or 0))
    if p <= 0:
        p = Decimal("0.5")
    return money / p


def portions(cny: float, unit: dict | None = None) -> dict:
    """元 → 份数。份数向下取整；剩下不到一份的零头单独给出。

    必须用 Decimal：二进制浮点里 11.0 // 0.1 是 109 不是 110、2.0 // 0.2 是 9 不是 10，
    单价填 0.1 / 0.2 / 0.3 这类数时会平白少数一份。
    """
    u = unit or unit_of()
    price = u["price"]
    money = Decimal(str(round(max(0.0, float(cny or 0)), 2)))
    if money <= 0:
        return {"count": 0, "price": price, "rest_cny": 0.0, "cny": 0.0}
    n = int(_div_money(money, price).to_integral_value(rounding=ROUND_DOWN))
    rest = money - Decimal(str(price)) * n
    return {
        "count": n,
        "price": price,
        "rest_cny": float(rest.quantize(Decimal("0.01"))),
        "cny": float(money),
    }


# ---------------- 流水 ----------------

def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(cny: float, ts: float) -> None:
    try:
        _plugin_data.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(
            json.dumps({"cny": round(cny, 2), "ts": ts, "iso": _iso(ts)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _append(entry: dict) -> None:
    try:
        _plugin_data.mkdir(parents=True, exist_ok=True)
        with open(_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _record(cny: float, *, price: float = 0.5, source: str = "sample", force: bool = False):
    """把这次余额和上次比对，有变化就记一笔。返回这一笔（没变化返回 None）。"""
    with _lock:
        st = _load_state()
        prev = st.get("cny")
        prev_ts = float(st.get("ts") or 0)
        now = time.time()
        entry = None

        if prev is None:
            entry = {
                "ts": now, "iso": _iso(now), "kind": "open",
                "prev_cny": None, "cny": round(cny, 2),
                "delta_cny": 0.0, "delta_bowls": 0.0,
                "gap_sec": 0, "source": source,
            }
        else:
            delta = round(round(cny, 2) - float(prev), 2)
            if force or abs(delta) >= _MIN_DELTA:
                entry = {
                    "ts": now, "iso": _iso(now),
                    "kind": "income" if delta > 0 else "expense",
                    "prev_cny": round(float(prev), 2), "cny": round(cny, 2),
                    "delta_cny": delta,
                    "delta_bowls": float((_div_money(abs(delta), price)
                                          * (1 if delta >= 0 else -1)
                                          ).quantize(Decimal("0.01"))),
                    "gap_sec": int(now - prev_ts) if prev_ts else 0,
                    "source": source,
                }

        if entry:
            _append(entry)
        # 只有真查到余额才推进状态，避免一次失败把基线冲掉
        _save_state(cny, now)
        return entry


def _read_entries(hours: float = 24.0) -> list[dict]:
    since = (time.time() - hours * 3600) if hours and hours > 0 else 0.0
    rows: list[dict] = []
    try:
        txt = _LEDGER.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return rows
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if float(e.get("ts") or 0) >= since:
            rows.append(e)
    rows.sort(key=lambda x: float(x.get("ts") or 0))
    return rows


def _human_window(hours: float) -> str:
    if hours <= 0:
        return "从一开始"
    if hours < 1:
        return "这 %d 分钟" % int(hours * 60)
    if hours < 24:
        return "这 %d 小时" % int(hours)
    d = hours / 24
    return "这 %d 天" % int(d) if d >= 1 else "这 24 小时"


def ledger_sync(hours: float = 24.0, limit: int = 20, unit: dict | None = None) -> dict:
    """最近一段时间的进账/消费流水。"""
    u = unit or unit_of()
    try:
        limit = max(1, min(int(limit or 20), 100))
    except Exception:  # noqa: BLE001
        limit = 20
    rows = _read_entries(hours)
    moves = [e for e in rows if e.get("kind") in ("income", "expense")]
    income_cny = round(sum(float(e.get("delta_cny") or 0) for e in moves if e.get("kind") == "income"), 2)
    expense_cny = round(-sum(float(e.get("delta_cny") or 0) for e in moves if e.get("kind") == "expense"), 2)
    # 份数统一按「当前配置的单价」由金额换算，这样换单位（碗→奶茶）后历史流水也对得上。
    # 不直接用 delta_bowls：那是按当时单价记的份数，单位一变含义就错了。
    price = u["price"]
    income = float(_div_money(income_cny, price).quantize(Decimal("0.01")))
    expense = float(_div_money(expense_cny, price).quantize(Decimal("0.01")))

    tail = moves[-limit:]
    entries = [{
        "iso": e.get("iso", ""),
        "kind": e.get("kind"),
        "delta_count": float(_div_money(abs(float(e.get("delta_cny") or 0)), price).quantize(Decimal("0.01")))
                        * (1 if float(e.get("delta_cny") or 0) >= 0 else -1),
        "delta_cny": float(e.get("delta_cny") or 0),
        "cny": float(e.get("cny") or 0),
    } for e in tail]

    win = _human_window(hours)
    m = u["measure"]
    if not moves:
        say = "%s没有进账也没有花掉。" % win
    elif income > 0 and expense > 0:
        say = "%s进了 %g %s（%.2f 元），吃掉 %g %s（%.2f 元），净 %s%g %s。" % (
            win, income, m, income_cny, expense, m, expense_cny,
            "+" if income - expense >= 0 else "", round(income - expense, 2), m)
    elif income > 0:
        say = "%s进了 %g %s（%.2f 元），还没怎么动。" % (win, income, m, income_cny)
    else:
        say = "%s吃掉 %g %s（%.2f 元），没进账，得省着点。" % (win, expense, m, expense_cny)

    return {
        "status": "ok",
        "window_hours": hours,
        "unit": unit_label(u),
        "income_count": income, "income_cny": income_cny,
        "expense_count": expense, "expense_cny": expense_cny,
        "net_count": round(income - expense, 2),
        "moves": len(moves),
        "entries": entries,
        "say": say,
    }


_ERR_HINT = {
    "no_api_key": "还没配 DeepSeek 的 API Key。去插件设置里填一个，或者先在「模型提供商」里配好 DeepSeek，插件会自己复用。",
    "http_401": "Key 被拒了（401），可能填错了，或者已经失效。",
    "http_403": "这个 Key 没有查余额的权限（403）。",
    "bad_json": "余额接口返回的不是能看懂的东西，可能 api_base 填到了别家的地址。",
}


async def report(api_key: str, api_base: str = _DEFAULT_BASE,
                 unit: dict | None = None, timeout: float = 12.0) -> dict:
    """查一次余额并换算成配置的单位，顺手落一笔账。"""
    u = unit or unit_of()
    b = await fetch_balance(api_key, api_base, timeout)
    if not b["ok"]:
        err = b["error"] or "unknown"
        return {
            "status": "error",
            "error": err,
            "message": _ERR_HINT.get(err, "余额这会儿没查成。"),
        }
    entry = await asyncio.to_thread(_record, b["cny"], price=u["price"], source="tool")
    r = portions(b["cny"], u)
    label = unit_label(u)
    m = u["measure"]
    if r["rest_cny"] > 0:
        say = "还剩 %d %s（%.2f 元，一%s %.2f 元，零头 %.2f 元）" % (
            r["count"], label, r["cny"], m, r["price"], r["rest_cny"])
    else:
        say = "还剩 %d %s（%.2f 元，一%s %.2f 元，正好凑整）" % (
            r["count"], label, r["cny"], m, r["price"])
    out = {
        "status": "ok",
        "unit": label,
        "count": r["count"],
        "price": r["price"],
        "cny": r["cny"],
        "rest_cny": r["rest_cny"],
        "say": say,
    }
    if entry and entry.get("kind") in ("income", "expense"):
        out["last_change"] = {
            "kind": entry["kind"],
            "delta_count": float(_div_money(abs(float(entry["delta_cny"])), u["price"]).quantize(Decimal("0.01")))
                           * (1 if float(entry["delta_cny"]) >= 0 else -1),
            "delta_cny": entry["delta_cny"], "iso": entry["iso"],
        }
    return out


# ---------------- 插件 ----------------

@register(PLUGIN_NAME, "li-0-li", "大肥鱼的白饭碗：把 DeepSeek 余额换算成几碗饭或几杯奶茶，并记进账/消费流水", "0.2.0")
class RiceBowlPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._sampler_task = None
        self._start_sampler()

    # ---- 取值 ----

    def _unit(self) -> dict:
        """读配置凑出一个单位描述。`unit_price` 没填时回落到老的 `bowl_price`。"""
        unit_price = self.config.get("unit_price")
        if unit_price in (None, "", 0, "0"):
            unit_price = self.config.get("bowl_price", 0.5)
        return unit_of(
            self.config.get("unit_measure", ""),
            self.config.get("unit_name", ""),
            unit_price,
        )

    def _price(self) -> float:
        return self._unit()["price"]

    def _timeout(self) -> float:
        try:
            return float(self.config.get("api_timeout", 12) or 12)
        except Exception:  # noqa: BLE001
            return 12.0

    def _enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    def _credential(self) -> tuple[str, str]:
        """按 手填 → 已配供应商 → 扫配置文件 的顺序找一个能用的 key。

        手填永远优先，这样用户想换成中转或者另一个号的 key，填进去就生效。
        """
        key = str(self.config.get("api_key") or "").strip()
        base = str(self.config.get("api_base") or "").strip()
        if key:
            return key, (base or _DEFAULT_BASE)
        k, b = auto_credential(self.context)
        if k:
            return k, (base or b or _DEFAULT_BASE)
        keys = _key_candidates()
        if keys:
            return keys[0], (base or _DEFAULT_BASE)
        return "", (base or _DEFAULT_BASE)

    # ---- 后台采样 ----

    def _start_sampler(self) -> None:
        if not self.config.get("enable_sampler", True):
            _log("后台采样已关闭")
            return
        try:
            interval = int(self.config.get("sample_interval", 600) or 600)
        except Exception:  # noqa: BLE001
            interval = 600
        self._sampler_task = asyncio.create_task(self._sampler_loop(max(60, interval)))
        key, base = self._credential()
        unit = self._unit()
        _log("后台采样已启动，间隔 %d 秒，单位=一%s %.2f 元，key=%s，base=%s"
             % (max(60, interval), unit_label(unit), unit["price"],
                mask_key(key) or "(未配置)", base))

    async def _sampler_loop(self, interval: int) -> None:
        await asyncio.sleep(_START_DELAY)
        while True:
            try:
                key, base = self._credential()
                b = await fetch_balance(key, base, self._timeout(), use_cache=False)
                if b["ok"]:
                    await asyncio.to_thread(_record, b["cny"], price=self._price(), source="timer")
                elif b["error"] == "no_api_key":
                    _log("采样跳过：还没配 key")
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _log("采样失败: %r" % exc)
            await asyncio.sleep(interval)

    # ---- 自查用：每轮对齐一下尺子，但不要念出来 ----

    _INJECT_TAG = "【我的饭碗 · 自查用】"

    def _inject_text(self) -> str:
        unit = self._unit()
        label = unit_label(unit)
        price = float(unit["price"])
        return "\n".join([
            "",
            self._INJECT_TAG,
            "换个算法：账户余额按「%s」来算，1 %s = %.2f 元。" % (label, label, price),
            "问你还有多少%s的时候，调 check_rice 拿实时余额再答。" % unit["name"],
            "这几行只给你自己对账用。正常聊天就行，"
            "不要把单价、量词、谁定的规矩这些说出口。",
            "也不要用市场价、常识价或账本里的旧数字去算。",
        ]) + "\n"

    @filter.on_llm_request()
    async def inject_unit(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """把主人的设定写进 system_prompt。

        她的认知来自人格里的旧说法，配置一改她不会自己知道，
        所以每轮都由插件跟她说一遍尺子怎么用、该去查而不是翻旧账。
        """
        if not self._enabled():
            return
        try:
            cur = req.system_prompt or ""
            if self._INJECT_TAG in cur:      # 工具循环里会重复触发，注一次就够
                return
            req.system_prompt = cur + self._inject_text()
        except Exception as exc:  # noqa: BLE001
            _log("注入设定失败: %r" % exc)

    # ---- 工具 ----

    @llm_tool(name="check_rice")
    async def check_rice(self, event: AstrMessageEvent, dummy: str = "") -> str:
        """查自己还剩几份口粮。按账户余额除以单价换算，向下取整。

        计价单位由插件配置决定，可能是白米饭、奶茶、糖里的任意一种，用户随时会改，
        所以**每次被问到都必须现场调一次**：不要沿用对话历史里的旧数字，也不要自己
        按旧单价心算——你猜不到当前配置的是什么单位、一份多少钱。
        用户问「还剩几碗饭」「还有多少奶茶」「还有钱吗」「碗空了吗」「够吃几顿」
        「余额多少」「还能撑几天」这类话时都用这个。
        返回 status / unit / count / price / cny / rest_cny / say。
        say 已经是按当前单位念好的整句，直接照着说，不要自己重新换算。
        用户点名了另一种计价物时（比如配置是白米饭、他偏问奶茶），
        拿返回里的 cny 乘除他给的单价再说，别拿旧的 count。

        Args:
            dummy(string): 不用填，留空即可。
        """
        if not self._enabled():
            return json.dumps({"status": "disabled", "message": "查碗功能已关闭。"}, ensure_ascii=False)
        try:
            key, base = self._credential()
            return json.dumps(await report(key, base, self._unit(), self._timeout()), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            _log("check_rice 失败: %r" % exc)
            return json.dumps({"status": "error", "message": repr(exc)}, ensure_ascii=False)

    @llm_tool(name="rice_ledger")
    async def rice_ledger(self, event: AstrMessageEvent, hours: int = 24, limit: int = 20) -> str:
        """查自己的进账和消费记录：什么时候多了几份、什么时候少了几份。

        单位同样由配置决定且随时会变，每次问都要现调，别拿历史里的数字。
        用户问「今天进账了吗」「这周吃了几碗」「碗什么时候空的」「最近花了多少」
        这类话时用这个。
        返回 status / unit / income_count / expense_count / net_count / entries / say，
        其中 say 已经是按当前单位念好的整句，直接照着说。

        Args:
            hours(number): 往前看多少小时，默认 24；想看更久就传 72、168。
            limit(number): 最多列几条明细，默认 20。
        """
        if not self._enabled():
            return json.dumps({"status": "disabled", "message": "查碗功能已关闭。"}, ensure_ascii=False)
        try:
            return json.dumps(
                await asyncio.to_thread(ledger_sync, float(hours or 24), int(limit or 20), self._unit()),
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            _log("rice_ledger 失败: %r" % exc)
            return json.dumps({"status": "error", "message": repr(exc)}, ensure_ascii=False)

    # ---- 手动命令（自己测的时候用） ----

    @filter.command("查碗", alias={"我的碗", "碗里还有多少", "查余额"})
    async def cmd_bowl(self, event: AstrMessageEvent):
        try:
            key, base = self._credential()
            r = await report(key, base, self._unit(), self._timeout())
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result("查不出来了: %r" % exc)
            return
        if r.get("status") != "ok":
            yield event.plain_result(r.get("message") or "余额这会儿没查成。")
            return
        yield event.plain_result(r["say"])

    @filter.command("查流水", alias={"饭账", "吃了多少"})
    async def cmd_ledger(self, event: AstrMessageEvent):
        try:
            r = await asyncio.to_thread(ledger_sync, 24.0, 20, self._unit())
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result("翻不出来了: %r" % exc)
            return
        yield event.plain_result(r["say"])

    async def terminate(self) -> None:
        if self._sampler_task:
            self._sampler_task.cancel()
            self._sampler_task = None
