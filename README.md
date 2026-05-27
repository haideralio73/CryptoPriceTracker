# Crypto Price Tracker

Real-time cryptocurrency dashboard for the terminal. Tracks top coins by market cap via the **CoinGecko API** (free, no API key needed).

## Features

- Live table of top coins — price, 24h change, 24h high/low, volume, candle change %
- Color-coded output — green for gains, red for losses
- OHLC candle aggregation — generate 1m / 15m / 4h / any candles from snapshots
- Alerts — flag coins that move beyond a % threshold
- CSV logging — snapshot data + candle data saved daily
- Fully configurable — CLI flags or `config.json`

## Quick Start

```bash
# 1. Install Python 3.10+
# 2. Install dependencies
pip install requests rich

# 3. Run it
python tracker.py

# Or with options:
python tracker.py --interval 60 --candle 900 --timeframe 15m --count 50 --alert 5
```

## Usage

```
python tracker.py [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--interval SEC` | `60` | Snapshot frequency in seconds |
| `--count N` | `10` | Number of coins to track (max 250) |
| `--alert PCT` | off | Warn when 24h change exceeds ±PCT% |
| `--candle SEC` | off | Aggregate snapshots into OHLC candles |
| `--timeframe LABEL` | — | Display label (e.g. `1m`, `15m`, `4h`) |
| `--csv-dir DIR` | `.` | Directory for CSV output |
| `--config FILE` | — | Load settings from JSON file |

### Examples

```bash
# Default: top 10 coins, refresh every 60s
python tracker.py

# 15-minute candles from 1m snapshots, top 50 coins
python tracker.py --interval 60 --candle 900 --timeframe 15m --count 50

# 4h candles from 5m snapshots, 10% alert
python tracker.py --interval 300 --candle 14400 --timeframe 4h --count 100 --alert 10

# 100 coins, no candles, 1m refresh
python tracker.py --interval 60 --count 100
```

## Configuration File

Save settings to `config.json` and load with `--config config.json`:

```json
{
    "refresh_seconds": 60,
    "coin_count": 50,
    "alert_threshold": 5.0,
    "candle_seconds": 900,
    "timeframe_label": "15m",
    "csv_dir": "./data"
}
```

CLI flags override values from the config file.

## Output

### Terminal Dashboard

```
+---------------------------------------------------------------------+
|                    CRYPTO PRICE TRACKER                              |
|            2026-05-27 12:30:00 UTC                                   |
|            Top 20 coins  |  60s snapshots  |  15m candles           |
+---------------------------------------------------------------------+
  # | Coin     | Price    | 24h Chg | Range (Hi/Lo)    | Volume  | Candle
----+----------+----------+---------+------------------+---------+-------
  1 | Bitcoin  | $75,826  | v -1.5% | $77,881 | $75,220 | $36.52B | 0.00%
  2 | Ethereum | $2,082   | v -1.6% | $2,135  | $2,058  | $14.79B | 0.00%
...
+-- Summary -----------------------------------------------------------+
| Total Market Cap $2.4T  |  Avg 24h Change -0.61%  |  ^ 2  v 18     |
+---------------------------------------------------------------------+
```

### CSV Files

Both files are created daily in the output directory:

| File | Content |
|---|---|
| `prices_2026-05-27.csv` | One row per coin per refresh — timestamp, price, 24h change, 24h high/low, volume, market cap, ATH change |
| `candles_2026-05-27.csv` | One row per coin per completed candle — open, high, low, close, volume, % change, snapshot count |

## Files to Upload

```
tracker.py              # main application
README.md               # this file
config.json             # optional — your settings
```

## Requirements

- Python 3.10+
- `requests` — HTTP client
- `rich` — terminal UI toolkit

Install with: `pip install requests rich`
