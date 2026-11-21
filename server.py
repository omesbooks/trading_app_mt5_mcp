"""MCP Terminal: a small local web app that shows MT5 charts and sends orders through MT5's MCP server.

Run:  ..\\repo\\.venv\\Scripts\\python.exe server.py      (or start_terminal.cmd)  ->  http://127.0.0.1:8790

Safety:
- listens on 127.0.0.1 only; every POST must carry the header X-MCP-Terminal: 1 and, when the browser sends an
  Origin, come from this app's own origin (stops other web pages from posting orders to it);
- every order is checked here before it leaves: symbol in the terminal, volume inside the symbol's min/max/step and
  the app's own max lot, SL/TP on the right side of the price, pending price on the right side of the market;
- "demo only" is on by default: orders are refused unless MT5 reports a demo account;
- each order request and MT5's answer is appended to ~/.mcp-terminal/journal.jsonl.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import mcp_client as mc

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("MCP_TERMINAL_PORT", "8790"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
HOME = Path(os.environ.get("MCP_TERMINAL_HOME") or Path.home() / ".mcp-terminal")
SETTINGS = HOME / "settings.json"
JOURNAL = HOME / "journal.jsonl"
DEFAULTS = {"url": os.environ.get("MCP_TERMINAL_URL") or mc.DEFAULT_URL, "demo_only": True, "max_volume": 1.0, "magic": 0, "comment": "MCP Terminal"}
TF_MIN = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080, "MN1": 43200}
SYMBOL = re.compile(r"^[A-Za-z0-9._#+\-!]{2,32}$")
PENDING = ("buy_limit", "sell_limit", "buy_stop", "sell_stop")

app = FastAPI(title="MCP Terminal", docs_url=None, redoc_url=None)
try:
    mc.copy_shared_once()          # first run: take over the web app's saved key
except OSError:
    pass
_client: mc.Client | None = None
_client_lock = threading.Lock()
_spec_cache: dict[str, tuple[float, dict]] = {}


# --- settings, journal ------------------------------------------------------------------------------

def settings() -> dict:
    try:
        saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(values: dict) -> dict:
    merged = {**settings(), **{k: v for k, v in values.items() if k in DEFAULTS and v is not None}}
    HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    return merged


def journal(entry: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    entry = {"time": datetime.now().isoformat(timespec="seconds"), **entry}
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def client() -> mc.Client:
    global _client
    with _client_lock:
        url = mc.check_url(settings()["url"])
        if _client is None or _client.url != url:
            _client = mc.Client(url)
        return _client


def call(name: str, args: dict | None = None) -> Any:
    try:
        return mc.value_of(client().call(name, args))
    except mc.McpError as exc:
        raise HTTPException(503, str(exc)) from None


# --- guard against other web pages -----------------------------------------------------------------

@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("x-mcp-terminal") != "1" or (origin and origin not in ORIGINS):
            return JSONResponse({"detail": "refused: not from this app"}, status_code=403)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def refused(request: Request, exc: HTTPException):
    """Order requests refused here (before or instead of MT5) go to the journal too, so the app's Journal tab
    shows why a button did nothing."""
    if request.method == "POST" and request.url.path.startswith(("/api/order", "/api/position")):
        journal({"kind": request.url.path.rsplit("/", 1)[-1], "tool": request.url.path, "ok": False,
                 "error": str(exc.detail)[:400]})
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def bad_body(request: Request, exc: RequestValidationError):
    text = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ())[1:])}: {e.get('msg')}" for e in exc.errors())[:400]
    if request.url.path.startswith(("/api/order", "/api/position")):
        journal({"kind": request.url.path.rsplit("/", 1)[-1], "tool": request.url.path, "ok": False,
                 "error": "bad request: " + text})
    return JSONResponse({"detail": "bad request: " + text}, status_code=422)


# --- helpers -----------------------------------------------------------------------------------------

def num(x: Any) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def parse_time(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), UTC).replace(tzinfo=None)
    text = str(raw).strip().replace("Z", "")
    if re.match(r"^\d{4}\.\d{2}\.\d{2}", text):
        text = text.replace(".", "-", 2)
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def epoch(dt: datetime) -> int:
    """Server wall time as if it were UTC (charts show the broker's clock, like MT5)."""
    return int(dt.replace(tzinfo=UTC).timestamp())


def plain_symbol(symbol: str) -> str:
    if not SYMBOL.match(symbol or ""):
        raise HTTPException(400, "not a symbol name")
    return symbol


def spec(symbol: str, fresh: bool = False) -> dict:
    """Specification + quote of one symbol (cached 1 s for quotes)."""
    symbol = plain_symbol(symbol)
    hit = _spec_cache.get(symbol)
    if hit and not fresh and time.time() - hit[0] < 1.0:
        return hit[1]
    rows = mc.rows_of(call("get_marketwatch_symbols", {"symbol": symbol}))
    row = next((r for r in rows if str(r.get("symbol") or r.get("name") or "").upper() == symbol.upper()), None)
    if not row:
        raise HTTPException(404, f"{symbol} is not in this terminal")
    _spec_cache[symbol] = (time.time(), row)
    return row


def server_now() -> datetime:
    info = call("get_time_information", {})
    last = parse_time(info.get("trade_server_last_known_time")) if isinstance(info, dict) else None
    return last or datetime.now(UTC).replace(tzinfo=None)


def account() -> dict:
    info = call("get_trading_account_info", {})
    acc = (info.get("account") if isinstance(info, dict) else None) or {}
    term = (info.get("terminal") if isinstance(info, dict) else None) or {}
    keep = {k: acc.get(k) for k in ("type", "currency", "balance", "equity", "margin", "margin_free", "margin_level",
                                    "profit", "leverage", "server", "company", "trade_allowed")}
    keep["mcp_trade_allowed"] = term.get("mcp_trade_allowed")
    keep["connected"] = term.get("server_connected")
    return keep


def first(row: dict, *keys: str) -> Any:
    return next((row[k] for k in keys if row.get(k) not in (None, "")), None)


def ticket_of(row: dict, *keys: str) -> int | None:
    value = num(first(row, *keys))
    return int(value) if value is not None else None


def norm_position(p: dict) -> dict:
    """One open position in this app's shape. MT5's MCP answers position_id / action / stop_loss / take_profit /
    price_last / create_time; other spellings are accepted too."""
    kind = str(first(p, "action", "type") or "").lower()
    return {"ticket": ticket_of(p, "position_id", "ticket", "position", "identifier"), "symbol": p.get("symbol"),
            "type": "buy" if "buy" in kind or kind == "0" else "sell",
            "volume": num(first(p, "volume", "volume_current", "open_volume")),
            "price_open": num(first(p, "price_open", "open_price")),
            "price_current": num(first(p, "price_last", "price_current")),
            "sl": num(first(p, "stop_loss", "sl")) or 0.0, "tp": num(first(p, "take_profit", "tp")) or 0.0,
            "profit": num(p.get("profit")), "swap": num(p.get("swap")),
            "time": first(p, "create_time", "time", "open_time", "time_open"), "comment": p.get("comment")}


def norm_order(o: dict) -> dict:
    kind = str(first(o, "action", "type", "order_type") or "").lower().replace("order_type_", "")
    return {"ticket": ticket_of(o, "order_id", "ticket", "order", "order_ticket", "position_id"), "symbol": o.get("symbol"),
            "type": kind, "volume": num(first(o, "volume", "volume_current", "volume_initial")),
            "price_open": num(first(o, "price_open", "price", "open_price")),
            "sl": num(first(o, "stop_loss", "sl")) or 0.0, "tp": num(first(o, "take_profit", "tp")) or 0.0,
            "time": first(o, "create_time", "time_setup", "time"), "comment": o.get("comment")}


def norm_closed(p: dict) -> dict:
    kind = str(first(p, "type", "action") or "").lower()
    return {"ticket": ticket_of(p, "position_id", "ticket", "position"), "symbol": p.get("symbol"),
            "type": "buy" if "buy" in kind or kind == "0" else "sell",
            "volume": num(first(p, "close_volume", "open_volume", "volume")),
            "open_price": num(first(p, "open_price", "price_open")), "close_price": num(first(p, "close_price", "price_close")),
            "open_time": first(p, "open_time", "time_open"), "close_time": first(p, "close_time", "time_close"),
            "close_reason": p.get("close_reason"), "profit": num(p.get("profit")), "comment": p.get("comment")}


def split_open(value: Any) -> tuple[list, list]:
    """(positions, pending orders) from get_trading_open_positions, in this app's shape."""
    if isinstance(value, dict) and ("positions" in value or "orders" in value):
        pos, ords = list(value.get("positions") or []), list(value.get("orders") or [])
    else:
        pos, ords = mc.rows_of(value), []
    return [norm_position(p) for p in pos], [norm_order(o) for o in ords]


# --- order checks ----------------------------------------------------------------------------------

def check_volume(volume: float, row: dict) -> float:
    s = settings()
    vmin, vmax, step = num(row.get("volume_min")) or 0.01, num(row.get("volume_max")) or 100.0, num(row.get("volume_step")) or 0.01
    if volume <= 0 or volume < vmin - 1e-9:
        raise HTTPException(400, f"volume {volume} is below the symbol's minimum {vmin}")
    if volume > vmax + 1e-9:
        raise HTTPException(400, f"volume {volume} is above the symbol's maximum {vmax}")
    if volume > float(s["max_volume"]) + 1e-9:
        raise HTTPException(400, f"volume {volume} is above this app's max lot {s['max_volume']} (Settings)")
    steps = round((volume - vmin) / step)
    if abs(vmin + steps * step - volume) > step * 1e-3:
        raise HTTPException(400, f"volume must be a multiple of {step} from {vmin}")
    return round(vmin + steps * step, 8)


# --- lot from risk -------------------------------------------------------------------------------------

_rate_cache: dict[tuple[str, str], tuple[float, float, str]] = {}


def _quote_mid(symbol: str) -> float | None:
    """Mid price of a symbol, showing it in Market Watch first when MT5 has it but streams no quote for it."""
    try:
        row = spec(symbol, fresh=True)
    except HTTPException:
        return None
    if not (num(row.get("bid")) and num(row.get("ask"))):
        try:
            client().call("add_marketwatch_symbol", fit_args("add_marketwatch_symbol", {"symbol": symbol, "show": True}))
            time.sleep(0.4)
            row = spec(symbol, fresh=True)
        except (mc.McpError, mc.Refused, HTTPException):
            return None
    bid, ask = num(row.get("bid")), num(row.get("ask"))
    return (bid + ask) / 2 if bid and ask else None


def ccy_rate(src: str, dst: str, suffix: str, depth: int = 0) -> tuple[float, str]:
    """How many `dst` one `src` is worth, from the terminal's own quotes (direct pair, inverse pair, or via USD)."""
    src, dst = (src or "").upper(), (dst or "").upper()
    if not src or not dst:
        raise HTTPException(409, "the symbol or the account has no currency to convert")
    if src == dst:
        return 1.0, "same currency"
    hit = _rate_cache.get((src, dst))
    if hit and time.time() - hit[0] < 15:
        return hit[1], hit[2]
    for sfx in dict.fromkeys((suffix, "")):
        for pair, invert in ((f"{src}{dst}{sfx}", False), (f"{dst}{src}{sfx}", True)):
            mid = _quote_mid(pair)
            if mid:
                rate = 1 / mid if invert else mid
                _rate_cache[(src, dst)] = (time.time(), rate, pair)
                return rate, pair
    if depth == 0 and "USD" not in (src, dst):
        a, pa = ccy_rate(src, "USD", suffix, 1)
        b, pb = ccy_rate("USD", dst, suffix, 1)
        _rate_cache[(src, dst)] = (time.time(), a * b, f"{pa} x {pb}")
        return a * b, f"{pa} x {pb}"
    raise HTTPException(409, f"cannot price {src} in {dst}: no {src}{dst} or {dst}{src} quote in this terminal")


def money_per_lot_unit(row: dict, acc_ccy: str) -> tuple[float, float, str]:
    """(account money per 1.0 price move per 1 lot, conversion rate, how) = contract size x profit-currency rate."""
    cs = num(row.get("contract_size") or row.get("trade_contract_size"))
    if not cs:
        raise HTTPException(409, "the symbol has no contract size: use a fixed lot")
    name, base, prof = str(row.get("symbol") or ""), str(row.get("currency_base") or ""), str(row.get("currency_profit") or "")
    suffix = name[len(base + prof):] if base and name.upper().startswith((base + prof).upper()) else ""
    rate, how = ccy_rate(prof, acc_ccy, suffix)
    return cs * rate, rate, how


class RiskIn(BaseModel):
    mode: Literal["percent", "money"]
    value: float = Field(gt=0)


def lot_for_risk(row: dict, entry: float, sl: float | None, risk: RiskIn, acc: dict) -> dict:
    """The largest lot whose loss at SL is within the risk (percent of balance or account money), on the symbol's
    step, between its minimum and both maximums. Raises when the SL is missing or the minimum lot risks too much."""
    if not sl:
        raise HTTPException(400, "risk sizing needs a stop loss")
    balance = num(acc.get("balance")) or 0
    money = balance * risk.value / 100 if risk.mode == "percent" else risk.value
    if risk.mode == "percent" and risk.value > 10:
        raise HTTPException(400, "risk above 10 % of balance per order is refused here")
    if money <= 0:
        raise HTTPException(409, "balance is 0: cannot size by percent")
    per_unit, rate, how = money_per_lot_unit(row, acc.get("currency") or "")
    distance = abs(entry - sl)
    if distance <= 0:
        raise HTTPException(400, "stop loss equals the entry price")
    loss_per_lot = distance * per_unit
    raw = money / loss_per_lot
    vmin, vmax, step = num(row.get("volume_min")) or 0.01, num(row.get("volume_max")) or 100.0, num(row.get("volume_step")) or 0.01
    if raw < vmin - 1e-9:
        raise HTTPException(400, f"risk {money:.2f} {acc.get('currency')} is less than the minimum lot {vmin} would "
                                 f"lose at this SL ({vmin * loss_per_lot:.2f}); widen the risk or tighten the SL")
    lots = vmin + math.floor((raw - vmin) / step + 1e-9) * step
    cap = min(vmax, float(settings()["max_volume"]))
    capped = lots > cap + 1e-9
    lots = round(min(lots, cap), 8)
    return {"volume": lots, "raw": raw, "risk_money": money, "loss_at_sl": lots * loss_per_lot,
            "currency": acc.get("currency"), "money_per_point": per_unit * (num(row.get("point")) or 0),
            "rate": rate, "rate_from": how, "capped": capped, "cap": cap, "balance": balance}


_acc_cache: list = [0.0, {}]


def account_cached() -> dict:
    if time.time() - _acc_cache[0] > 5:
        _acc_cache[:] = [time.time(), account()]
    return _acc_cache[1]


def check_demo() -> dict:
    acc = account()
    if settings()["demo_only"] and acc.get("type") != "demo":
        raise HTTPException(403, f"demo only is on, and MT5 reports a {acc.get('type') or 'unknown'} account. "
                                 "Turn it off in Settings only if you mean to trade real money.")
    if acc.get("mcp_trade_allowed") is False:
        raise HTTPException(403, "MT5 does not allow trading through MCP: enable it in MT5 (AI Assistant / MCP "
                                 "settings > allow trading) and Algo Trading.")
    return acc


def check_levels(side: str, entry_buy: float, entry_sell: float, sl: float | None, tp: float | None, row: dict) -> None:
    """SL below / TP above the price for a buy, the other way for a sell, and at least the stops level away."""
    point = num(row.get("point")) or 0.0
    gap = (num(row.get("trade_stops_level")) or 0) * point
    if side == "buy":
        if sl is not None and not sl < entry_sell - gap:
            raise HTTPException(400, f"SL {sl} must be below {entry_sell - gap:.10g} for a buy")
        if tp is not None and not tp > entry_buy + gap:
            raise HTTPException(400, f"TP {tp} must be above {entry_buy + gap:.10g} for a buy")
    else:
        if sl is not None and not sl > entry_buy + gap:
            raise HTTPException(400, f"SL {sl} must be above {entry_buy + gap:.10g} for a sell")
        if tp is not None and not tp < entry_sell - gap:
            raise HTTPException(400, f"TP {tp} must be below {entry_sell - gap:.10g} for a sell")


def round_price(x: float | None, row: dict) -> float | None:
    if x is None:
        return None
    digits = int(num(row.get("digits")) or 5)
    return round(x, digits)


def fit_args(tool: str, wanted: dict) -> dict:
    """Keep only the arguments the tool's schema lists, map enum values, and refuse when a required one is missing."""
    schema = client().schemas().get(tool) or {}
    props = schema.get("properties") or {}
    args: dict = {}
    for key, value in wanted.items():
        if value is None:
            continue
        name = key if key in props else next((p for p in props if p.lower() == key.lower()), None)
        if name is None and key == "position_ticket":
            name = next((p for p in props if p in ("ticket", "position")), None)
        if name is None and key == "order_ticket":
            name = next((p for p in props if p in ("ticket", "order")), None)
        if name is None:
            continue
        enum = props[name].get("enum")
        if enum and isinstance(value, str):
            norm = lambda s: re.sub(r"[^a-z]", "", str(s).lower()).replace("ordertype", "")
            value = next((e for e in enum if norm(e) == norm(value)), value)
            if value not in enum:
                raise HTTPException(400, f"{tool}: '{key}' must be one of {enum}")
        args[name] = value
    missing = [r for r in schema.get("required") or [] if r not in args]
    if missing:
        raise HTTPException(400, f"{tool} needs {missing}; the app could not fill them")
    return args


def send(tool: str, args: dict, what: dict) -> dict:
    try:
        ok, reply, ticket = mc.outcome(client().call(tool, args))
    except mc.McpError as exc:
        journal({"tool": tool, "args": args, **what, "ok": False, "error": str(exc)})
        raise HTTPException(503, str(exc)) from None
    journal({"tool": tool, "args": args, **what, "ok": ok, "ticket": ticket, "reply": reply})
    _spec_cache.clear()
    return {"ok": ok, "ticket": ticket, "reply": reply}


# --- API: status, settings, key --------------------------------------------------------------------

class KeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=400)


class SettingsIn(BaseModel):
    url: str | None = None
    demo_only: bool | None = None
    max_volume: float | None = Field(default=None, gt=0, le=1000)
    magic: int | None = Field(default=None, ge=0, le=2**31 - 1)
    comment: str | None = Field(default=None, max_length=24)


@app.get("/api/status")
def status() -> dict:
    out: dict = {"key": mc.key_status(), "settings": settings(), "connected": False}
    try:
        c = client()
        out["account"] = account()
        out["server"] = c.server
        out["connected"] = True
        out["tools"] = sorted(c.schemas())
        out["server_time"] = server_now().isoformat(timespec="seconds")
    except HTTPException as exc:
        out["error"] = exc.detail
    except mc.BadUrl as exc:
        out["error"] = str(exc)
    return out


@app.post("/api/key")
def set_key(body: KeyIn) -> dict:
    mc.store_token(body.key)
    if _client:
        _client.reset()
    return status()


class ResetIn(BaseModel):
    all_settings: bool = False       # also max lot, magic, comment and demo only back to their defaults


@app.post("/api/reset")
def reset(body: ResetIn) -> dict:
    """Disconnect from the MT5 MCP server: end the session, delete this app's saved key, server URL back to the
    default. Nothing in MT5 changes (open positions and orders stay); the web app's key is not touched."""
    global _client
    with _client_lock:
        if _client:
            _client.reset()
        _client = None
    _spec_cache.clear()
    removed = mc.forget_token()
    if body.all_settings:
        HOME.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(DEFAULTS, indent=1), encoding="utf-8")
    else:
        save_settings({"url": DEFAULTS["url"]})
    journal({"kind": "reset", "tool": "/api/reset", "ok": True,
             "reply": {"comment": f"key removed: {removed}; settings reset: {body.all_settings}"}})
    out = {"key_removed": removed, "settings": settings(), "key": mc.key_status()}
    if out["key"]["set"]:
        out["note"] = "MT5_MCP_TOKEN is set in the environment, so a key is still in use until it is unset."
    return out


@app.post("/api/settings")
def set_settings(body: SettingsIn) -> dict:
    values = body.model_dump()
    if values.get("url"):
        try:
            values["url"] = mc.check_url(values["url"])
        except mc.BadUrl as exc:
            raise HTTPException(400, str(exc)) from None
    saved = save_settings(values)
    if _client:
        _client.reset()
    return saved


@app.get("/api/schema/{tool}")
def tool_schema(tool: str) -> dict:
    if tool not in mc.ALLOWED:
        raise HTTPException(404, "not an allowed tool")
    return client().schemas().get(tool) or {}


# --- API: market data ------------------------------------------------------------------------------

def symbol_view(r: dict) -> dict:
    return {"symbol": r.get("symbol") or r.get("name"), "description": r.get("description"),
            "bid": num(r.get("bid")), "ask": num(r.get("ask")), "digits": r.get("digits"), "point": num(r.get("point")),
            "spread": r.get("spread"), "time": r.get("time"), "volume_min": num(r.get("volume_min")),
            "volume_max": num(r.get("volume_max")), "volume_step": num(r.get("volume_step")),
            "contract_size": num(r.get("contract_size") or r.get("trade_contract_size")),
            "stops_level": r.get("trade_stops_level"), "currency_profit": r.get("currency_profit"),
            "change": num(r.get("price_change") or r.get("daily_change")), "trade_mode": r.get("trade_mode")}


@app.get("/api/symbols")
def symbols() -> list:
    rows = mc.rows_of(call("get_marketwatch_symbols", {"limit": 1000}))
    return [symbol_view(r) for r in rows]


@app.get("/api/quote/{symbol}")
def quote(symbol: str) -> dict:
    return symbol_view(spec(symbol, fresh=True))


@app.get("/api/candles/{symbol}")
def candles(symbol: str, tf: str = "M15", bars: int = 400, since: int | None = None) -> dict:
    symbol = plain_symbol(symbol)
    if tf not in TF_MIN:
        raise HTTPException(400, f"timeframe must be one of {list(TF_MIN)}")
    bars = max(20, min(int(bars), 3000))
    end = server_now() + timedelta(days=1)
    if since:
        start = datetime.fromtimestamp(since, UTC).replace(tzinfo=None) - timedelta(minutes=TF_MIN[tf])
    else:
        span = timedelta(minutes=TF_MIN[tf] * bars * 1.5) + timedelta(days=3 if TF_MIN[tf] < 1440 else 10)
        start = end - span
    reply = call("get_chart_history", {"symbol": symbol, "period": tf,
                                       "datetime_from": start.isoformat(timespec="seconds"),
                                       "datetime_to": end.isoformat(timespec="seconds")})
    out = []
    for r in mc.rows_of(reply):
        t = parse_time(r.get("time"))
        o, h, lo, c = (num(r.get(k)) for k in ("open", "high", "low", "close"))
        if t and None not in (o, h, lo, c):
            out.append({"time": epoch(t), "open": o, "high": h, "low": lo, "close": c,
                        "volume": num(r.get("tick_volume") or r.get("volume")) or 0})
    out.sort(key=lambda b: b["time"])
    dedup = {b["time"]: b for b in out}
    bars_out = list(dedup.values())[-bars:] if not since else list(dedup.values())
    return {"symbol": symbol, "tf": tf, "bars": bars_out}


@app.get("/api/account")
def account_view() -> dict:
    return account()


@app.get("/api/open")
def open_trades() -> dict:
    positions, orders = split_open(call("get_trading_open_positions", {}))
    return {"positions": positions, "orders": orders}


@app.get("/api/history")
def history(days: int = 7) -> dict:
    now = server_now()
    args = {"datetime_from": (now - timedelta(days=max(1, min(days, 90)))).isoformat(timespec="seconds"),
            "datetime_to": (now + timedelta(days=1)).isoformat(timespec="seconds")}
    rows = [norm_closed(r) for r in mc.rows_of(call("get_trading_history_positions", args))]
    rows.sort(key=lambda r: str(r.get("close_time") or ""), reverse=True)
    return {"positions": rows[:300]}


@app.get("/api/journal")
def journal_view(limit: int = 100) -> list:
    try:
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 1000)):]
    except OSError:
        return []
    return [json.loads(x) for x in reversed(lines) if x.strip()]


# --- API: orders -------------------------------------------------------------------------------------

class MarketIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    volume: float | None = Field(default=None, gt=0)      # fixed lot, or
    risk: RiskIn | None = None                            # lot computed here from the risk and the SL at send time
    sl: float | None = Field(default=None, gt=0)
    tp: float | None = Field(default=None, gt=0)
    comment: str | None = Field(default=None, max_length=24)


def order_volume(body: MarketIn, row: dict, entry: float, sl: float | None) -> tuple[float, dict | None]:
    if body.risk:
        sized = lot_for_risk(row, entry, sl, body.risk, account_cached())
        return check_volume(sized["volume"], row), sized
    if not body.volume:
        raise HTTPException(400, "give a volume or a risk")
    return check_volume(body.volume, row), None


class SizeIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    sl: float = Field(gt=0)
    price: float | None = Field(default=None, gt=0)       # pending price; market uses the live ask / bid
    risk: RiskIn


@app.post("/api/size")
def size(body: SizeIn) -> dict:
    """Preview of the lot a risk would give (the order endpoints compute it again when sending)."""
    row = spec(body.symbol, fresh=True)
    bid, ask = num(row.get("bid")), num(row.get("ask"))
    entry = body.price or (ask if body.side == "buy" else bid)
    if not entry:
        raise HTTPException(409, f"{body.symbol} has no live quote")
    return {"entry": entry, **lot_for_risk(row, entry, body.sl, body.risk, account_cached())}


class PendingIn(MarketIn):
    """Buy or sell at `price`. Limit or Stop is chosen here from the live quote at send time;
    `kind`, when given, must agree with it."""
    side: Literal["buy", "sell"] | None = None
    kind: Literal["buy_limit", "sell_limit", "buy_stop", "sell_stop"] | None = None
    price: float = Field(gt=0)


def pending_kind(side: str, price: float, bid: float, ask: float) -> str:
    """Buy below the ask = buy_limit, above = buy_stop; sell above the bid = sell_limit, below = sell_stop."""
    ref = ask if side == "buy" else bid
    if price == ref:
        raise HTTPException(400, f"pending price {price} equals the market ({'ask' if side == 'buy' else 'bid'}); "
                                 "move it above or below")
    below = price < ref
    return f"{side}_{'limit' if below == (side == 'buy') else 'stop'}"


class ModifyIn(BaseModel):
    symbol: str
    ticket: int
    sl: float | None = Field(default=None, ge=0)
    tp: float | None = Field(default=None, ge=0)


class CloseIn(BaseModel):
    symbol: str
    ticket: int
    volume: float | None = Field(default=None, gt=0)


class DeleteIn(BaseModel):
    symbol: str | None = None
    ticket: int


def _comment(text: str | None) -> str:
    return (text if text is not None else settings()["comment"])[:24]


@app.post("/api/order/market")
def order_market(body: MarketIn) -> dict:
    acc = check_demo()
    row = spec(body.symbol, fresh=True)
    bid, ask = num(row.get("bid")), num(row.get("ask"))
    if not bid or not ask:
        raise HTTPException(409, f"{body.symbol} has no live quote (market closed or symbol hidden)")
    sl, tp = round_price(body.sl, row), round_price(body.tp, row)
    check_levels(body.side, ask, bid, sl, tp, row)
    volume, sized = order_volume(body, row, ask if body.side == "buy" else bid, sl)
    args = fit_args("trade_send_market_order", {"symbol": row.get("symbol") or body.symbol, "type": body.side,
                                                "volume": volume, "sl": sl, "tp": tp, "comment": _comment(body.comment),
                                                "magic": settings()["magic"] or None})
    out = send("trade_send_market_order", args, {"kind": "market", "account": acc.get("type"), "bid": bid, "ask": ask,
                                                  "sized": sized})
    return {**out, "volume": volume, "sized": sized}


@app.post("/api/order/pending")
def order_pending(body: PendingIn) -> dict:
    acc = check_demo()
    row = spec(body.symbol, fresh=True)
    bid, ask = num(row.get("bid")), num(row.get("ask"))
    if not bid or not ask:
        raise HTTPException(409, f"{body.symbol} has no live quote")
    price = round_price(body.price, row)
    side = body.side or (body.kind.split("_")[0] if body.kind else None)
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side (buy or sell) is required")
    kind = pending_kind(side, price, bid, ask)
    if body.kind and body.kind != kind:
        where = {"buy_limit": f"below ask {ask}", "sell_limit": f"above bid {bid}",
                 "buy_stop": f"above ask {ask}", "sell_stop": f"below bid {bid}"}[body.kind]
        raise HTTPException(400, f"{body.kind.replace('_', ' ')} price must be {where}")
    gap = (num(row.get("trade_stops_level")) or 0) * (num(row.get("point")) or 0)
    if gap and abs(price - (ask if side == "buy" else bid)) < gap:
        raise HTTPException(400, f"pending price is closer to the market than the broker's stops level ({gap:.10g})")
    sl, tp = round_price(body.sl, row), round_price(body.tp, row)
    check_levels(side, price, price, sl, tp, row)
    volume, sized = order_volume(body, row, price, sl)
    args = fit_args("trade_send_pending_order", {"symbol": row.get("symbol") or body.symbol, "type": kind,
                                                 "order_type": kind, "volume": volume, "price": price, "sl": sl,
                                                 "tp": tp, "comment": _comment(body.comment),
                                                 "magic": settings()["magic"] or None})
    out = send("trade_send_pending_order", args, {"kind": kind, "account": acc.get("type"), "bid": bid, "ask": ask,
                                                   "sized": sized})
    return {**out, "kind": kind, "volume": volume, "sized": sized}


def _find_position(ticket: int) -> dict:
    positions, _ = split_open(call("get_trading_open_positions", {}))
    for p in positions:
        if p["ticket"] == ticket:
            return p
    raise HTTPException(404, f"no open position #{ticket} (it may have closed already)")


@app.post("/api/position/modify")
def position_modify(body: ModifyIn) -> dict:
    """New SL/TP for an open position. An empty value removes it (0). MT5 asks that unchanged values be left out,
    so only the side that changed is sent."""
    check_demo()
    pos = _find_position(body.ticket)
    row = spec(pos["symbol"] or body.symbol, fresh=True)
    bid, ask = num(row.get("bid")), num(row.get("ask"))
    sl = round_price(body.sl, row) if body.sl else 0.0
    tp = round_price(body.tp, row) if body.tp else 0.0
    point = num(row.get("point")) or 1e-9
    changed = {k: v for k, v in (("sl", sl), ("tp", tp)) if abs(v - pos[k]) > point / 2}
    if not changed:
        raise HTTPException(400, "SL and TP are the same as now: nothing to change")
    if bid and ask:
        px = bid if pos["type"] == "buy" else ask          # the price the position would close at
        check_levels(pos["type"], px, px, changed.get("sl") or None, changed.get("tp") or None, row)
    args = fit_args("trade_modify_sl_tp", {"symbol": pos["symbol"] or body.symbol, "position_ticket": body.ticket, **changed})
    return send("trade_modify_sl_tp", args, {"kind": "modify"})


@app.post("/api/position/close")
def position_close(body: CloseIn) -> dict:
    check_demo()
    pos = _find_position(body.ticket)
    args = fit_args("trade_close_single_position", {"symbol": pos["symbol"] or body.symbol,
                                                    "position_ticket": body.ticket, "volume": body.volume})
    return send("trade_close_single_position", args, {"kind": "close"})


@app.post("/api/order/modify")
def order_modify(body: ModifyIn) -> dict:
    """New SL/TP for a pending order (trade_modify_sl_tp with order_ticket). Only the changed side is sent; an
    empty value removes it (0). The order's own price is checked against: SL below / TP above it for a buy."""
    check_demo()
    _, orders = split_open(call("get_trading_open_positions", {}))
    order = next((o for o in orders if o["ticket"] == body.ticket), None)
    if not order:
        raise HTTPException(404, f"no pending order #{body.ticket} (it may have filled or been deleted)")
    row = spec(order["symbol"] or body.symbol, fresh=True)
    side = "buy" if "buy" in order["type"] else "sell"
    entry = order["price_open"]
    sl = round_price(body.sl, row) if body.sl else 0.0
    tp = round_price(body.tp, row) if body.tp else 0.0
    point = num(row.get("point")) or 1e-9
    changed = {k: v for k, v in (("sl", sl), ("tp", tp)) if abs(v - order[k]) > point / 2}
    if not changed:
        raise HTTPException(400, "SL and TP are the same as now: nothing to change")
    if entry:
        check_levels(side, entry, entry, changed.get("sl") or None, changed.get("tp") or None, row)
    args = fit_args("trade_modify_sl_tp", {"symbol": order["symbol"] or body.symbol, "order_ticket": body.ticket,
                                           **changed})
    return send("trade_modify_sl_tp", args, {"kind": "modify-order"})


@app.post("/api/order/delete")
def order_delete(body: DeleteIn) -> dict:
    check_demo()
    args = fit_args("trade_delete_order", {"order_ticket": body.ticket, "symbol": body.symbol})
    return send("trade_delete_order", args, {"kind": "delete"})


# --- page ----------------------------------------------------------------------------------------------

@app.get("/")
def page() -> FileResponse:
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
