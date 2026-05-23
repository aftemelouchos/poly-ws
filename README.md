# Polymarket Live Strategy v2 (Stock RSI)

UP token mid fiyatı üzerinden **RSI cross** sinyalleri ile **FAK** emirleri kullanan
düşük gecikmeli (HFT odaklı) Polymarket trading botu.

* Market verisi: **WebSocket** (order book + price_change + best_bid_ask + last_trade_price)
* Pozisyon takibi: **User WebSocket** (MATCHED/CONFIRMED trade event'leri ile)
* REST yalnızca *yedek* (WS kitap boşsa snapshot, başlangıç bakiyesi, rollover)
* `buy_fast` / `sell_fast` çağrıları **milisaniye** olarak loglanır

```
[BUY_UP] BUY x 6.0 | pos=UP | rsi=47 | 245ms
[SELL_UP] SELL x 6.0 | 198ms
```

## Hızlı başlangıç (Ubuntu)

### 1. Sistem hazırlığı
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git build-essential \
    libffi-dev libssl-dev tzdata
sudo timedatectl set-timezone Europe/Istanbul   # opsiyonel
```

### 2. Repo klonla + kur
```bash
cd ~
git clone https://github.com/aftemelouchos/poly-ws.git
cd poly-ws
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. .env oluştur
```bash
cp .env.example .env
nano .env   # PRIVATE_KEY ve FUNDER_ADDRESS gir
```

### 4. Manuel çalıştır (test)
```bash
chmod +x scripts/run_live.sh
./scripts/run_live.sh
```

## systemd ile arka plan servisi

```bash
sudo cp scripts/polybot.service /etc/systemd/system/polybot.service
# Eger kullanici/path farkliysa duzenle:
sudo nano /etc/systemd/system/polybot.service

sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot

# Canli log
sudo journalctl -u polybot -f -n 100
# veya:
tail -f logs/live_strategy_v2.log
```

Durdur/yeniden başlat:
```bash
sudo systemctl restart polybot
sudo systemctl stop polybot
```

## tmux ile basit alternatif

```bash
sudo apt -y install tmux
tmux new -s bot
cd ~/poly-ws && ./scripts/run_live.sh
# Detach: Ctrl-b sonra d
# Geri don: tmux attach -t bot
```

## CLI parametreleri

```
--rsi-period 8           # RSI periyodu
--cross-up 45            # alttan yukari kesince UP al
--cross-down 55          # ustten asagi kesince DOWN al
-k 6                     # her islemde share sayisi
--res-force 1            # son P dk: pozisyon kapatma penceresi
--res-hold 0.70          # mid >= bu -> son P dk satma, settlement bekle
--res-dump 0.50          # mid <  bu -> son P dk hemen dump
--dry-run                # live client ama emir gondermez
```

Strateji ayarlarinin tamamı `config/strategy_v2_live.yaml` icinde.

## Dosya yapisi

```
poly-ws/
  run_live_strategy_v2.py        # tek calistirilabilir (live)
  requirements.txt               # live runtime (minimum)
  requirements-dev.txt           # + pandas + matplotlib (backtest icin)
  .env.example
  config/
    strategy_v2_live.yaml        # strateji + resolution parametreleri
    trading.yaml                 # trading altyapi (WS URL, paper init)
  scripts/
    run_live.sh                  # Linux launcher
    polybot.service              # systemd unit
    backtest_strategy_v2.py      # tek CSV backtest
    batch_backtest_strategy_v2.py# data/ws/5m toplu, kumulatif PnL
    optimize_strategy_v2.py      # grid search (RSI x cross)
    visualize_strategy_v2.py     # 5 panel grafik (UP/DOWN/RSI/PnL/tablo)
  src/
    config.py                    # WebSocketConfig
    models.py                    # OrderBook, BookState, Fill, ...
    market_resolver.py           # Gamma API (slug -> tokens)
    ws_market.py                 # public market WS
    ws_user.py                   # authenticated user WS
    ws_csv_loader.py             # backtest icin CSV loader
    live_strategy_v2/
      engine.py                  # ana canli motor
      config.py
      rsi_stream.py              # canli mid -> bar -> RSI
    strategy_v2/
      config.py                  # StockRsiConfig
      resolution.py              # son P dk kurallari
      rsi.py                     # RSI hesap
      types.py                   # Side enum
      backtest.py                # backtest motoru (pandas)
      cli_helpers.py             # ortak CLI argumanlari
    trading/
      hft_runtime.py             # WS + buy_fast / sell_fast
      gateway.py                 # paper + live (CLOB) trading
      pricing.py                 # FAK taker fiyat hesabi
      execution.py               # FAK hata yorumlama, min notional
      book_levels.py             # WS price_change -> kitap guncelleme
      book_sync.py               # REST kitap snapshot (fallback)
      fill_wait.py               # fill onayi yedek bakiye poll
      clob_client.py             # CLOB v2 client factory
      factory.py
      config.py
      types.py
```

## Backtest / Optimize / Visualize (opsiyonel — live icin sart degil)

`scripts/` klasoru data/ws/5m CSV'lerini kullanarak parametre arar ve grafik
cikarir. Sadece **gelistirme** icin, sunucuda kurmana gerek yok.

```bash
pip install -r requirements-dev.txt   # pandas + matplotlib
```

### Tek dosya backtest
```bash
python scripts/backtest_strategy_v2.py \
  -f data/ws/5m/btc-updown-5m-1779358800_*.csv \
  --rsi-period 8 --cross-up 45 --cross-down 55 -k 6 \
  --res-force 1 --res-hold 0.7 --res-dump 0.5
```

### Toplu backtest (N pencere, kumulatif PnL)
```bash
python scripts/batch_backtest_strategy_v2.py --n 30 --k 6 \
  --rsi-period 8 --cross-up 45 --cross-down 55 \
  --res-force 1 --res-hold 0.7 --res-dump 0.5 \
  --plot charts/batch.png
```

### Grid search (RSI + cross seviyeleri)
```bash
python scripts/optimize_strategy_v2.py --n 30 -k 6 \
  --rsi-min 6 --rsi-max 16 --rsi-step 2 \
  --up-min 30 --up-max 50 --down-min 50 --down-max 70 --step 5 \
  --res-force 1 --res-hold 0.7 --res-dump 0.5 \
  --heatmap charts/heatmap.png
```

### Grafik (UP/DOWN + RSI + PnL + islem listesi)
```bash
python scripts/visualize_strategy_v2.py \
  -f data/ws/5m/btc-updown-5m-1779358800_*.csv \
  --rsi-period 8 --cross-up 45 --cross-down 55 -k 6 \
  --save charts/v2.png --no-show
```

## Performans notlari

- `buy_fast` / `sell_fast` cogunlukla **WS-only** calisir (REST yalnizca kitap bos kalirsa)
- Trade onayi User WS `MATCHED/CONFIRMED` event'i ile (~10-100ms), REST poll yedek
- Periyodik bakiye senkronu varsayilan 60s — sadece dogrulama, ana yol degil
- Loglarda her trade icin **wall-clock ms** suresi yazilir (request -> WS confirm)
