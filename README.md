# MCP Terminal

A small local web app that talks to MetaTrader 5's built-in MCP server: Market Watch quotes, candlestick charts,
and a trading panel that sends orders into MT5. No Expert Advisor is involved; MT5's Journal shows each order with
Source `127.0.0.1` (the MCP server).

## Run

1. In MT5: Tools > Options > MCP > enable the internal server, allow trading through MCP, and turn on Algo Trading.
2. Windows, Python 3.11+. Once: `python -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`.
   Then double-click `start_terminal.cmd` (uses `.venv` here, else `..\repo\.venv` of the TradingAgents web app,
   else `python` on PATH), or run `.venv\Scripts\python.exe server.py`. It opens http://127.0.0.1:8790.
3. First time: ⚙ Settings > paste the API Key from the top of MT5's MCP tab > "บันทึก & ทดสอบ".
   The key is stored encrypted with Windows DPAPI in `~/.mcp-terminal/mt5_mcp_token.dpapi` (this app only, never in
   this folder). If the TradingAgents web app has a saved key, it is copied once on the first start; after that the
   two apps keep separate keys.
4. To unplug the connected MT5 (or switch terminal): Settings > "⟲ Reset การเชื่อมต่อ" ends the MCP session, deletes
   this app's key and sets the URL back to `http://127.0.0.1:22346/mcp`; the tick box also restores max lot, magic,
   comment and demo only. Positions and orders in MT5 are not touched, nor is the web app's key.

## What it does

| Part | Detail |
|---|---|
| Market Watch | symbols shown in MT5 Market Watch, bid/ask every 3 s, click to select |
| Chart | candles from `get_chart_history` (M1..D1, 500 bars, new bars every 2 s), broker server time; lines for open positions (entry / SL / TP), pending orders, the SL/TP/price being typed, and the ask |
| Trading panel | Market BUY/SELL at the live bid/ask; Buy pending / Sell pending, with Limit or Stop chosen from the price (buy below ask = Buy Limit, above = Buy Stop; sell above bid = Sell Limit, below = Sell Stop), shown live and decided again by the server from the quote at send time; volume with the symbol's step; SL/TP as price or points; ⌖ picks a price from the chart; estimated risk / target / R:R in the account currency (when the symbol's profit currency is the account currency); confirm dialog (on by default) |
| Lot size | "Lot คงที่" (type the lot), "Risk %" (of balance, at most 10 %) or "Risk เงิน" (account currency). In the risk modes the lot = risk ÷ (SL distance × contract size × profit-currency rate), rounded down to the symbol's step, kept between its minimum and both maximums (symbol, app max lot); the rate comes from the terminal's own quote (e.g. USDCHF for AUDCHF on a USD account, showing the pair in Market Watch if needed, or via USD). The panel previews it (POST /api/size); the server computes it again from the live price when the order is sent. An SL is required; a risk smaller than the minimum lot's loss is refused |
| Draggable lines | "📏 เส้น SL/TP ฝั่ง BUY / SELL" (market) or "📏 แสดงเส้น SL/TP" (pending) puts SL at 1.5 × and TP at 3 × the average bar range of the last 14 bars and draws them thick with ↕; drag them (and the pending price line) on the chart, the fields and the risk estimate follow. Dragging an open position's or a pending order's SL or TP line opens a confirm box; OK sends `trade_modify_sl_tp`, Cancel puts the line back. A second click on the same side button (or "✕ ซ่อนเส้น SL/TP" for pending) hides all draft lines, the pending price line too; the price stays in its field |
| Bottom tabs | positions (edit SL/TP, close), pending orders (edit SL/TP, delete), 7-day history, the app's own order journal |

## Safety

- Listens on 127.0.0.1 only. POSTs need the header `X-MCP-Terminal: 1` and, when the browser sends an Origin, this
  app's own origin, so another web page cannot post orders to it.
- MCP client allowlist (`mcp_client.ALLOWED`): read tools, the five order tools (market, pending, modify SL/TP,
  close one position, delete one order) and showing a symbol. Tester, chart, file and web-request tools are refused
  before a request is made. The MT5 server URL must be on this computer.
- Each order is checked before it is sent: symbol known to the terminal, live quote, volume within the symbol's
  min/max/step and the app's max lot (Settings, default 1.0), SL/TP on the correct side (plus the stops level),
  pending price on the correct side of the market.
- **Demo only** is on by default: orders are refused unless MT5 reports a demo account. Turning it off asks for
  confirmation.
- Every order request and MT5's reply is appended to `~/.mcp-terminal/journal.jsonl`.

## Files

- `server.py`: FastAPI app and order checks. Env overrides for testing: `MCP_TERMINAL_PORT`, `MCP_TERMINAL_HOME`,
  `MCP_TERMINAL_URL`, `MT5_MCP_TOKEN`.
- `mcp_client.py`: MCP JSON-RPC client (session reuse, allowlist, DPAPI key).
- `index.html`: the page (TradingView lightweight-charts 4.2.0 from unpkg, so the first load needs internet).

## Known limits

- Risk sizing uses contract size x price move, which is how MT5 prices forex and CFD symbols; symbols priced by
  tick value (futures-style calc modes) may differ.

- A pending order's own price cannot be changed: MT5's MCP offers no tool for it (trade_modify_sl_tp changes SL/TP
  only). Delete it and place a new one.

- Argument names for pending orders, close and delete are taken from the tool schemas MT5 publishes (`tools/list`)
  and were tested against a stand-in server, not yet against the real terminal.
- Risk estimate is shown only when the symbol's profit currency equals the account currency (e.g. XAUUSD, EURUSD on
  a USD account); cross pairs show "-".
- Polling, not streaming: quotes 1 s, bars 2 s, positions 2 s.
