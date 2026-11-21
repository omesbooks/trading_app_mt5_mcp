"""Client for MetaTrader 5's built-in MCP server (Tools > Options > MCP), for the MCP Terminal app.

Only a server on this computer (127.0.0.1 / localhost / ::1) is accepted, so the API key never
leaves the machine. The key is read from MT5_MCP_TOKEN or from this app's own DPAPI-encrypted file
(~/.mcp-terminal/mt5_mcp_token.dpapi). On first run the TradingAgents web app's key
(~/.tradingagents/secrets/mt5_mcp_token.dpapi) is copied once, so nothing has to be pasted again; after that
the two apps keep separate keys, and resetting this one never touches the web app's. The key is never
returned, logged or echoed in errors.

Every tool goes through `call`, which refuses anything not on ALLOWED: market data and account
reads, plus the five order tools the trading panel needs. Tester, chart, file, web-request and
Market Watch-removal tools are never called.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:22346/mcp"
TOKEN_ENV = "MT5_MCP_TOKEN"
APP_HOME = Path(os.environ.get("MCP_TERMINAL_HOME") or Path.home() / ".mcp-terminal")
SECRET_PATH = APP_HOME / "mt5_mcp_token.dpapi"
SHARED_PATH = Path.home() / ".tradingagents" / "secrets" / "mt5_mcp_token.dpapi"   # TradingAgents web app's key
SHARED_CHECKED = APP_HOME / ".shared_key_copied"     # the one-time copy happened (or a reset declined it)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

READ_TOOLS = frozenset({
    "get_chart_history", "get_marketwatch_symbols", "get_trading_account_info", "get_trading_open_positions",
    "get_trading_history_positions", "get_trading_history_orders", "get_time_information"})
TRADE_TOOLS = frozenset({
    "trade_send_market_order", "trade_send_pending_order", "trade_modify_sl_tp",
    "trade_close_single_position", "trade_delete_order"})
# Showing a symbol in Market Watch is needed before its quotes stream; removing is never called.
WATCH_TOOLS = frozenset({"add_marketwatch_symbol"})
ALLOWED = READ_TOOLS | TRADE_TOOLS | WATCH_TOOLS
ACCEPTED_RETCODES = {10008, 10009, 10010}      # placed, done, done partially


class Refused(Exception):
    """A call this client will not make."""


class BadUrl(ValueError):
    pass


# --- the key (Windows DPAPI, current user) ------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(function: str, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI exists only on Windows")
    buffer = ctypes.create_string_buffer(data, len(data))
    given, out = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), _Blob()
    if not getattr(ctypes.windll.crypt32, function)(ctypes.byref(given), None, None, None, None, 0x1,
                                                    ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def clean_token(raw: str) -> str:
    """The key alone, also when a copied settings line (`"Authorization": "Bearer <key>"`) was pasted."""
    text = (raw or "").strip().strip("'\",")
    found = re.search(r"bearer\s+([^\s\"',]+)", text, re.IGNORECASE)
    return found.group(1) if found else text


def store_token(token: str) -> None:
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_PATH.write_bytes(_dpapi("CryptProtectData", clean_token(token).encode("utf-8")))


def saved_token() -> str:
    try:
        return _dpapi("CryptUnprotectData", SECRET_PATH.read_bytes()).decode("utf-8")
    except (OSError, ValueError):
        return ""


def copy_shared_once() -> None:
    """First run only: take over the TradingAgents web app's saved key (still encrypted, same Windows user)."""
    if SHARED_CHECKED.exists():
        return
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if not SECRET_PATH.exists() and SHARED_PATH.exists():
        SECRET_PATH.write_bytes(SHARED_PATH.read_bytes())
    SHARED_CHECKED.write_text("1", encoding="utf-8")


def forget_token() -> bool:
    """Delete this app's saved key (the web app's key is left alone). True when a file was removed."""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    SHARED_CHECKED.write_text("1", encoding="utf-8")       # do not copy the web app's key back in
    if SECRET_PATH.exists():
        SECRET_PATH.unlink()
        return True
    return False


def find_token() -> str:
    return clean_token(os.environ.get(TOKEN_ENV, "")) or saved_token()


def key_status() -> dict:
    if clean_token(os.environ.get(TOKEN_ENV, "")):
        return {"set": True, "source": "environment variable MT5_MCP_TOKEN"}
    if saved_token():
        return {"set": True, "source": "saved on this computer (encrypted, this app only)"}
    return {"set": False, "source": None}


def check_url(url: str | None) -> str:
    url = (url or "").strip() or DEFAULT_URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BadUrl("The server URL must look like http://127.0.0.1:22346/mcp")
    if parsed.hostname.lower() not in LOOPBACK:
        raise BadUrl("Only a server on this computer is allowed (127.0.0.1 or localhost)")
    return url


def port_owner(port: int) -> str | None:
    """Install folder of the program listening on 127.0.0.1:<port> (Windows), e.g. 'MetaTrader 5 EXNESS'.

    Several MT5 terminals can be open, but only the first one gets the MCP port; the others log a bind error.
    A key copied from another terminal is then refused, so name the one that answers."""
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=5, check=False).stdout
        pid = next((ln.split()[-1] for ln in out.splitlines()
                    if f":{port} " in ln and "LISTEN" in ln.upper()), None)
        if not pid or not pid.isdigit():
            return None
        ps = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid}).Path"],
                            capture_output=True, text=True, timeout=8, check=False).stdout.strip()
        return Path(ps).parent.name if ps else f"process {pid}"
    except (OSError, subprocess.SubprocessError, StopIteration):
        return None


# --- the client ------------------------------------------------------------------------------------

class McpError(RuntimeError):
    """The server could not be reached or answered with an error. Message is safe to show."""


class Client:
    """One MCP session, reused across calls; calls are serialised (MT5 handles one at a time well)."""

    def __init__(self, url: str | None = None, timeout: float = 20) -> None:
        self.url = check_url(url)
        self.timeout = timeout
        self.session: str | None = None
        self.server: dict = {}
        self._id = 0
        self._lock = threading.Lock()
        self._schemas: dict[str, dict] = {}

    def _safe(self, text: str) -> str:
        token = find_token()
        return text.replace(token, "***") if token else text

    def _post(self, payload: dict) -> dict | None:
        token = find_token()
        if not token:
            raise McpError("No API key yet: open Settings, paste the key from MT5 Tools > Options > MCP and save.")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                body = resp.read().decode("utf-8", "replace")
                kind = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                owner = port_owner(urlparse(self.url).port or 80)
                where = f"The MCP port is held by: {owner}. Copy the key from THAT terminal. " if owner else ""
                raise McpError(f"MT5 refused the API key (HTTP {exc.code}). {where}Copy the API Key at the top of "
                               "MT5 Tools > Options > MCP, paste it in Settings and save. A key generated while MT5 "
                               "runs works only after OK and a terminal restart.") from None
            if exc.code == 404 and self.session:
                self.session = None          # session expired on the server
            raise McpError(f"MT5 answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise McpError("Nothing answered at %s. Is MetaTrader 5 running with Tools > Options > MCP > "
                           "'Enable internal server' on? (%s)" % (self.url, self._safe(str(getattr(exc, "reason", exc)))[:100])) from None
        if not body.strip():
            return None
        if "text/event-stream" in kind:
            data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            return json.loads(data[-1]) if data else None
        return json.loads(body)

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        reply = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if not reply:
            raise McpError(f"{method}: empty reply")
        if "error" in reply:
            raise McpError(self._safe(f"{method}: {reply['error']}")[:300])
        return reply.get("result") or {}

    def _ensure(self) -> None:
        if self.session is None:
            info = self._request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                "clientInfo": {"name": "mcp-terminal", "version": "1"}})
            self.server = info.get("serverInfo") or {}
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def schemas(self) -> dict[str, dict]:
        """inputSchema of each allowed tool the server offers (read once per session)."""
        with self._lock:
            self._ensure()
            if not self._schemas:
                tools = self._request("tools/list").get("tools") or []
                self._schemas = {t["name"]: t.get("inputSchema") or {} for t in tools if t.get("name") in ALLOWED}
            return dict(self._schemas)

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name not in ALLOWED:
            raise Refused(f"{name} is not on this app's list")
        with self._lock:
            for attempt in (1, 2):
                self._ensure()
                try:
                    return self._request("tools/call", {"name": name, "arguments": arguments or {}})
                except McpError:
                    if attempt == 2 or self.session is not None:
                        raise
            raise McpError("unreachable")

    def reset(self) -> None:
        with self._lock:
            self.session, self._schemas = None, {}


def value_of(result: dict) -> Any:
    """The JSON a tool returned (or its text, cut)."""
    text = "\n".join(c.get("text", "") for c in result.get("content") or () if c.get("type") == "text")
    try:
        return json.loads(text)
    except ValueError:
        return text[:800]


def rows_of(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return next((v for v in value.values() if isinstance(v, list)), [])
    return []


def outcome(result: dict) -> tuple[bool, Any, Any]:
    """(accepted, reply, ticket) of an order tool's result."""
    value = value_of(result)
    ok = not result.get("isError")
    ticket = None
    if isinstance(value, dict):
        if value.get("ok") is False or value.get("success") is False:
            ok = False
        code = value.get("retcode")
        if code is None and isinstance(value.get("result"), dict):
            code = value["result"].get("retcode")
        try:
            if code is not None and int(code) not in ACCEPTED_RETCODES:
                ok = False
        except (TypeError, ValueError):
            pass
        for key in ("order", "deal", "position", "ticket", "order_ticket", "position_ticket"):
            if value.get(key):
                ticket = value[key]
                break
    elif isinstance(value, str) and re.search(r"error|fail|invalid|reject|not allowed|not permitted", value, re.IGNORECASE):
        ok = False
    return ok, value, ticket
