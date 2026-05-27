"""
Crypto Price Tracker
--------------------
Tracks top cryptocurrencies by market cap using the CoinGecko API.

Features:
  - Rich terminal dashboard with styled tables and panels
  - 24h high/low range, volume, rank changes, price trend spark
  - Auto-refresh with countdown
  - OHLC candle aggregation for 1m / 15m / 4h / any timeframe
  - Snapshot CSV + OHLC candle CSV logging
  - --alert flag to warn on large moves
  - Configurable via CLI flags or config.json
"""

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from rich.align import Align
from rich.box import ASCII
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style

# ── Console ──────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
console = Console(highlight=False, width=120)


# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TrackerConfig:
    refresh_seconds: int = 60
    coin_count: int = 10
    alert_threshold: float = 0.0
    csv_dir: str = "."
    candle_seconds: int = 0          # 0 = no candle aggregation
    timeframe_label: str = ""        # display label (1m, 15m, 4h, ...)

    @classmethod
    def from_cli(cls) -> "TrackerConfig":
        parser = argparse.ArgumentParser(
            description="Crypto Price Tracker - live dashboard from CoinGecko",
        )
        parser.add_argument("--alert", type=float, default=0.0, metavar="PCT",
                            help="Warn when 24h change exceeds PCT (e.g. --alert 5)")
        parser.add_argument("--interval", type=int, default=60, metavar="SEC",
                            help="Refresh interval in seconds (default: 60)")
        parser.add_argument("--count", type=int, default=10, metavar="N",
                            help="Number of coins to track (default: 10, max 250)")
        parser.add_argument("--csv-dir", type=str, default=".", metavar="DIR",
                            help="Directory for CSV logs (default: current dir)")
        parser.add_argument("--candle", type=int, default=0, metavar="SEC",
                            help="OHLC candle period in seconds (e.g. 900 for 15m)")
        parser.add_argument("--timeframe", type=str, default="", metavar="LABEL",
                            help="Timeframe label for display (e.g. 1m, 15m, 4h)")
        parser.add_argument("--config", type=str, default="", metavar="FILE",
                            help="Path to config.json (CLI flags override)")
        args = parser.parse_args()

        cfg = cls()

        if args.config:
            cfg._load_json(args.config)

        if args.alert:
            cfg.alert_threshold = args.alert
        if args.interval != 60:
            cfg.refresh_seconds = args.interval
        if args.count != 10:
            cfg.coin_count = min(args.count, 250)
        if args.csv_dir != ".":
            cfg.csv_dir = args.csv_dir
        if args.candle:
            cfg.candle_seconds = args.candle
        if args.timeframe:
            cfg.timeframe_label = args.timeframe

        return cfg

    def _load_json(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.refresh_seconds = data.get("refresh_seconds", self.refresh_seconds)
            self.coin_count = data.get("coin_count", self.coin_count)
            self.alert_threshold = data.get("alert_threshold", self.alert_threshold)
            self.csv_dir = data.get("csv_dir", self.csv_dir)
            self.candle_seconds = data.get("candle_seconds", self.candle_seconds)
            self.timeframe_label = data.get("timeframe_label", self.timeframe_label)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            console.print(f"[yellow]Warning: could not load config {path}: {e}[/]")


# ═══════════════════════════════════════════════════════════════════════════════
#  Snapshot CSV logger (per-day files)
# ═══════════════════════════════════════════════════════════════════════════════

SNAP_HEADERS = [
    "timestamp", "coin_id", "name", "symbol",
    "rank", "price_usd", "change_24h_pct",
    "high_24h", "low_24h", "volume_usd",
    "market_cap_usd", "ath_change_pct",
]


class SnapshotLogger:
    """Logs one row per coin per refresh cycle to prices_YYYY-MM-DD.csv."""

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, coins: list[dict]) -> None:
        now_ts = datetime.now(timezone.utc).isoformat()
        path = self._path()
        new_file = not path.is_file()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(SNAP_HEADERS)
            for c in coins:
                w.writerow([
                    now_ts,
                    c.get("id", ""), c.get("name", ""), c.get("symbol", ""),
                    c.get("market_cap_rank") or 0,
                    c.get("current_price") or 0.0,
                    c.get("price_change_percentage_24h") or 0.0,
                    c.get("high_24h") or 0.0,
                    c.get("low_24h") or 0.0,
                    c.get("total_volume") or 0,
                    c.get("market_cap") or 0,
                    c.get("ath_change_percentage") or 0.0,
                ])

    def _path(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._dir / f"prices_{today}.csv"


# ═══════════════════════════════════════════════════════════════════════════════
#  OHLC Candle aggregator & logger
# ═══════════════════════════════════════════════════════════════════════════════

CANDLE_HEADERS = [
    "candle_start", "coin_id", "name", "symbol",
    "open", "high", "low", "close",
    "volume_usd", "change_pct", "snapshots",
]

# Per-coin candle state: start_ts, open, high, low, close, volume, snap_count, name, symbol
CandleState = dict[str, list[float | int | str]]


class CandleLogger:
    """Aggregates multiple snapshots into OHLC candles and logs them.

    Candle period is defined by *candle_sec* (e.g. 900 for 15m candles).
    Each time a candle boundary is crossed, the completed candle is flushed
    to candles_YYYY-MM-DD.csv and a new one begins.
    """

    def __init__(self, directory: str, candle_sec: int) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._period = candle_sec
        self._coins: CandleState = {}

    # ── public API ────────────────────────────────────────────────────────

    def update(self, coins: list[dict]) -> None:
        """Feed a fresh snapshot into the candle builder.  Flushes completed
        candles and starts new ones when a period boundary is crossed."""
        now = time.time()
        boundary = self._current_boundary(now)

        # Flush any coin whose boundary changed
        for cid in list(self._coins.keys()):
            if self._coins[cid][0] != boundary:
                self._flush(cid)

        # Upsert current snapshot into active candles
        for c in coins:
            cid = c.get("id", "")
            name = c.get("name", "")
            sym = c.get("symbol", "")
            price = c.get("current_price") or 0.0
            vol = c.get("total_volume") or 0

            if cid not in self._coins:
                self._coins[cid] = [boundary, price, price, price, price, vol, 1, name, sym]
            else:
                _, o, _, _, _, v, n = self._coins[cid]
                self._coins[cid] = [
                    boundary, o,
                    max(o, price),            # high
                    min(self._coins[cid][3], price),  # low
                    price,                    # close
                    max(v, vol),              # volume (use latest)
                    int(n) + 1,
                    name, sym,
                ]

    def current(self, cid: str) -> dict | None:
        """Return current candle data for a coin (for display)."""
        if cid not in self._coins:
            return None
        _, o, h, l_, c, v, n = self._coins[cid][:7]
        return {"open": o, "high": h, "low": l_, "close": c, "volume": v, "snaps": int(n)}

    def flush_all(self) -> None:
        """Force-flush all in-flight candles (e.g. on shutdown)."""
        for cid in list(self._coins.keys()):
            self._flush(cid)

    # ── internals ─────────────────────────────────────────────────────────

    def _current_boundary(self, ts: float) -> float:
        return (ts // self._period) * self._period

    def _flush(self, cid: str) -> None:
        c = self._coins.pop(cid, None)
        if c is None:
            return
        start, o, h, l_, close, v, n, name, sym = c
        if n == 0:
            return
        now = datetime.now(timezone.utc).isoformat()
        path = self._dir / f"candles_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"
        new_file = not path.is_file()
        chg = ((close - o) / o * 100) if o else 0.0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(CANDLE_HEADERS)
            w.writerow([now, cid, name, sym, o, h, l_, close, v, round(chg, 2), int(n)])

    def flush_for_coin(self, coins: list[dict]) -> None:
        """Flush candles for coins not in the latest batch (ranked out)."""
        active = {c.get("id", "") for c in coins}
        for cid in list(self._coins.keys()):
            if cid not in active:
                self._flush(cid)


# ═══════════════════════════════════════════════════════════════════════════════
#  CoinGecko API client
# ═══════════════════════════════════════════════════════════════════════════════

class CoinGeckoClient:
    BASE = "https://api.coingecko.com/api/v3/coins/markets"

    def fetch(self, count: int = 10) -> list[dict]:
        url = (
            f"{self.BASE}?vs_currency=usd&order=market_cap_desc"
            f"&per_page={count}"
        )
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            console.print(f"[red]API error:[/] {exc}")
            return []


# ═══════════════════════════════════════════════════════════════════════════════
#  Rendering helpers
# ═══════════════════════════════════════════════════════════════════════════════

STY_HDR   = Style(bold=True, color="cyan")
STY_UP    = Style(color="green", bold=True)
STY_DOWN  = Style(color="red", bold=True)
STY_FLAT  = Style(color="yellow")
STY_DIM   = Style(color="bright_black")
STY_BOLD  = Style(bold=True, color="white")


def _chg_text(val: float) -> Text:
    style = STY_UP if val > 0 else (STY_DOWN if val < 0 else STY_FLAT)
    sign = "+" if val > 0 else ""
    return Text(f"{sign}{val:.2f}%", style=style)


def _icon(val: float) -> str:
    return "^" if val > 0 else ("v" if val < 0 else "*")


def _fmt_price(p: float) -> str:
    if p >= 1:
        return f"${p:,.2f}"
    s = f"${p:.6f}".rstrip("0")
    return s if s[-1] != "." else s + "0"


def _fmt_compact(n: float | int) -> str:
    n = float(n)
    if n >= 1_000_000_000_000:
        return f"${n / 1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:,.0f}"


def _trend_spark(history: list[float], width: int = 5) -> Text:
    if len(history) < 2:
        return Text("---", style=STY_DIM)
    samples = history[-width:]
    chars = []
    for i in range(1, len(samples)):
        if samples[i] > samples[i - 1]:
            chars.append(("^", STY_UP))
        elif samples[i] < samples[i - 1]:
            chars.append(("v", STY_DOWN))
        else:
            chars.append(("-", STY_FLAT))
    while len(chars) < width:
        chars.insert(0, (".", STY_DIM))
    return Text.assemble(*[(c, s) for c, s in chars[-width:]])


def _rank_change(prev: int | None, curr: int | None) -> Text:
    if prev is None or curr is None:
        return Text("-", style=STY_DIM)
    if curr < prev:
        return Text(f"^+{prev - curr}", style=STY_UP)
    if curr > prev:
        return Text(f"v-{curr - prev}", style=STY_DOWN)
    return Text("=", style=STY_FLAT)


def _candle_pct(o: float, c: float) -> Text:
    """Return the % change inside the current candle (close vs open)."""
    if o == 0:
        return Text("--", style=STY_DIM)
    pct = (c - o) / o * 100
    style = STY_UP if pct > 0 else (STY_DOWN if pct < 0 else STY_FLAT)
    sign = "+" if pct > 0 else ""
    return Text(f"{sign}{pct:.2f}%", style=style)


# ═══════════════════════════════════════════════════════════════════════════════
#  Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

class Dashboard:
    def __init__(self, cfg: TrackerConfig, api: CoinGeckoClient) -> None:
        self.cfg = cfg
        self.api = api
        self.snap_logger = SnapshotLogger(directory=cfg.csv_dir)
        self.candle_logger = (
            CandleLogger(directory=cfg.csv_dir, candle_sec=cfg.candle_seconds)
            if cfg.candle_seconds
            else None
        )

        self._history: dict[str, list[float]] = {}
        self._prev_ranks: dict[str, int] = {}

    # ── refresh cycle ─────────────────────────────────────────────────────

    def refresh(self) -> list[dict]:
        coins = self.api.fetch(self.cfg.coin_count)
        if not coins:
            return []

        self.snap_logger.write(coins)

        for c in coins:
            cid = c.get("id", "")
            price = c.get("current_price") or 0.0
            self._history.setdefault(cid, []).append(price)
            rank = c.get("market_cap_rank")
            if rank is not None and cid not in self._prev_ranks:
                self._prev_ranks[cid] = rank

        if self.candle_logger:
            self.candle_logger.update(coins)

        return coins

    def close(self) -> None:
        if self.candle_logger:
            self.candle_logger.flush_all()

    # ── layout builders ───────────────────────────────────────────────────

    def _header(self) -> Panel:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts = [
            Text("CRYPTO PRICE TRACKER\n", style="bold cyan underline"),
            Text(f"{now}\n", style=STY_DIM),
            Text(f"Top {self.cfg.coin_count} coins  |  ", style="white"),
            Text(f"{self.cfg.refresh_seconds}s snapshots", style=STY_DIM),
        ]
        if self.cfg.timeframe_label:
            parts.append(Text(f"  |  ", style="white"))
            parts.append(Text(f"{self.cfg.timeframe_label} candles", style="bold green"))
        if self.candle_logger:
            parts.append(Text(f"  |  ", style="white"))
            parts.append(Text(f"{self.cfg.candle_seconds}s period", style=STY_DIM))
        return Panel(
            Align.center(Text.assemble(*parts)),
            box=ASCII, border_style="cyan", padding=(1, 2),
        )

    def _table(self, coins: list[dict]) -> Table:
        tbl = Table(box=ASCII, header_style=STY_HDR, padding=(0, 1), show_edge=False)
        tbl.add_column("#", justify="right", no_wrap=True)
        tbl.add_column("Coin", style="cyan", no_wrap=True)
        tbl.add_column("Price", justify="right", no_wrap=True)
        tbl.add_column("24h Chg", justify="right", no_wrap=True)
        tbl.add_column("Range (Hi/Lo)", justify="center", no_wrap=True)
        tbl.add_column("Volume", justify="right", no_wrap=True)
        tbl.add_column("Candle Chg", justify="right", no_wrap=True)

        for idx, c in enumerate(coins, start=1):
            cid   = c.get("id", "")
            name  = c.get("name", "?")
            price = c.get("current_price") or 0.0
            chg   = c.get("price_change_percentage_24h") or 0.0
            hi    = c.get("high_24h") or 0.0
            lo    = c.get("low_24h") or 0.0
            vol   = c.get("total_volume") or 0
            rank  = c.get("market_cap_rank")
            prev_rank = self._prev_ranks.get(cid)

            # Candle OHLC display
            candle_data = self.candle_logger.current(cid) if self.candle_logger else None
            candle_chg = (
                _candle_pct(candle_data["open"], candle_data["close"])
                if candle_data
                else Text("-", style=STY_DIM)
            )

            row = [
                str(idx),
                name,
                Text(_fmt_price(price), style=STY_BOLD),
                Text.assemble((_icon(chg) + " ", STY_DIM), _chg_text(chg)),
                Text(f"{_fmt_price(hi)} | {_fmt_price(lo)}", style=STY_DIM),
                _fmt_compact(vol),
                candle_chg,
            ]
            tbl.add_row(*row)

            if rank is not None:
                self._prev_ranks[cid] = rank

        return tbl

    def _summary(self, coins: list[dict]) -> Panel:
        total_mcap = sum(c.get("market_cap") or 0 for c in coins)
        changes    = [c.get("price_change_percentage_24h") or 0 for c in coins]
        avg_chg    = sum(changes) / max(len(changes), 1)
        green_ct   = sum(1 for c in changes if c > 0)
        red_ct     = sum(1 for c in changes if c < 0)
        flat_ct    = sum(1 for c in changes if c == 0)
        avg_style  = STY_UP if avg_chg > 0 else (STY_DOWN if avg_chg < 0 else STY_FLAT)

        content = Group(
            Text.assemble(Text("Total Market Cap  ", style="bright_white"),
                          Text(f"${total_mcap:,.0f}", style="bold cyan")),
            Text.assemble(Text("Avg 24h Change    ", style="bright_white"),
                          Text(f"{avg_chg:+.2f}%", style=avg_style)),
            Text.assemble(Text("Distribution      ", style="bright_white"),
                          Text(f"^ {green_ct}  ", style="green"),
                          Text(f"v {red_ct}  ", style="red"),
                          Text(f"* {flat_ct}", style="yellow") if flat_ct else Text("")),
        )
        if self.candle_logger:
            content.renderables.append(
                Text.assemble(
                    Text("Active candles    ", style="bright_white"),
                    Text(f"{self.cfg.candle_seconds}s period", style="bold yellow"),
                )
            )
        return Panel(content, box=ASCII, border_style="cyan",
                     title="[bold cyan]Summary[/]", title_align="left", padding=(1, 2))

    def _alerts(self, coins: list[dict]) -> Panel | None:
        threshold = self.cfg.alert_threshold
        if not threshold:
            return None
        triggered = [c for c in coins if abs(c.get("price_change_percentage_24h") or 0) > threshold]
        if not triggered:
            return None
        lines = []
        for c in triggered:
            chg = c.get("price_change_percentage_24h") or 0.0
            icon = "^" if chg > 0 else "v"
            style = "green" if chg > 0 else "red"
            lines.append(Text.assemble(
                Text(f"  {c.get('name','?'):20s}  ", style="bold"),
                Text(f"{icon}  ", style=style),
                Text(f"{chg:+.2f}%  ", style=f"bold {style}"),
                Text(f"(threshold: +/-{threshold}%)", style=STY_DIM),
            ))
        return Panel(Group(*lines), box=ASCII, border_style="yellow",
                     title="[bold yellow]! ALERTS[/]", title_align="left", padding=(1, 2))


# ═══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    cfg = TrackerConfig.from_cli()
    api = CoinGeckoClient()
    dash = Dashboard(cfg, api)

    try:
        while True:
            coins = dash.refresh()
            if not coins:
                console.print("[red]No data received - retrying...[/]")
                time.sleep(cfg.refresh_seconds)
                continue

            os.system("cls" if os.name == "nt" else "clear")
            console.print(dash._header())
            console.print(dash._table(coins))
            console.print(dash._summary(coins))
            alert = dash._alerts(coins)
            if alert:
                console.print(alert)

            try:
                time.sleep(cfg.refresh_seconds)
            except KeyboardInterrupt:
                raise

    except KeyboardInterrupt:
        dash.close()
        console.print("\n[yellow]Tracker stopped. Data saved to CSV.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
