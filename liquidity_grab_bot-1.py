"""
=============================================================
  LİKİDİTE KAPMA SİNYAL BOTU
  - CoinGecko + Binance Futures Top 50 Hacim Tarayıcı
  - Zaman Dilimleri: 1H / 2H / 4H
  - Telegram Bildirim
  - Her 10 dakikada bir tarama
  - API KEY GEREKMİYOR
=============================================================
"""

import time
import logging
import requests
import pandas as pd

# ─────────────────────────────────────────
#  AYARLAR
# ─────────────────────────────────────────
TELEGRAM_TOKEN   = "8407067459:AAGgGmH9jA6TwWHY-H62n6s9SKl3Bv0r1Mg"
TELEGRAM_CHAT_ID = "623705923"

TIMEFRAMES = {
    "1H": "1h",
    "2H": "2h",
    "4H": "4h",
}

MIN_BODY_PCT = 0.003
TOP_N        = 50
SCAN_EVERY   = 600

BINANCE_BASE = "https://fapi.binance.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# ─────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

sent_signals = {}


# ─────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code == 200:
            log.info("📨 Telegram bildirimi gönderildi.")
        else:
            log.warning(f"Telegram hatası: {resp.text}")
    except Exception as e:
        log.error(f"Telegram bağlantı hatası: {e}")


# ─────────────────────────────────────────
#  EN HACİMLİ 50 COİNİ ÇEK
#  CoinGecko hacim sıralaması + Binance Futures filtresi
# ─────────────────────────────────────────
def get_top_symbols(n=TOP_N):
    try:
        # 1. Binance Futures'da aktif işlem gören tüm sembolleri al
        futures_resp = requests.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        futures_symbols = set(
            s["symbol"] for s in futures_resp.json().get("symbols", [])
            if s["symbol"].endswith("USDT") and s.get("status") == "TRADING"
        )
        log.info(f"Binance Futures aktif sembol sayısı: {len(futures_symbols)}")

        # 2. CoinGecko'dan hacme göre sıralı coin listesi al
        cg_resp = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false"
            },
            timeout=15
        )
        coins = cg_resp.json()

        # 3. CoinGecko listesinden Binance Futures'da olanları filtrele
        symbols = []
        for coin in coins:
            sym = coin["symbol"].upper() + "USDT"
            if sym in futures_symbols:
                symbols.append(sym)
            if len(symbols) >= n:
                break

        log.info(f"📊 Top {len(symbols)} coin alındı (CoinGecko). İlk 5: {symbols[:5]}")
        return symbols

    except Exception as e:
        log.error(f"Sembol listesi alınamadı: {e}")
        return []


# ─────────────────────────────────────────
#  MUM VERİSİ ÇEK
# ─────────────────────────────────────────
def get_candles(symbol, interval):
    try:
        resp = requests.get(f"{BINANCE_BASE}/fapi/v1/klines", params={
            "symbol": symbol,
            "interval": interval,
            "limit": 100
        }, timeout=10)
        raw = resp.json()
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close",
            "volume","close_time","qav","trades","tbav","tqav","ignore"
        ])
        for col in ["open","high","low","close"]:
            df[col] = df[col].astype(float)
        df = df[:-1]  # Kapanmamış son mumu dahil etme
        return df.reset_index(drop=True)
    except Exception as e:
        log.error(f"[{symbol}] Mum verisi alınamadı: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────
#  SİNYAL TESPİTİ
# ─────────────────────────────────────────
def detect_signal(df):
    """
    SHORT:
      1. Yeşil ana mum (i)
      2. Hemen sonraki mum (i+1) KIRMIZI olmalı ve high > ana mumun high (likidite alındı)
      3. i+2'den itibaren herhangi bir mum: close < ana mumun low → SİNYAL
      Giriş: ana mumun low

    LONG:
      1. Kırmızı ana mum (i)
      2. Hemen sonraki mum (i+1) YEŞİL olmalı ve low < ana mumun low (likidite alındı)
      3. i+2'den itibaren herhangi bir mum: close > ana mumun high → SİNYAL
      Giriş: ana mumun high
    """
    if df.empty:
        return None, None

    for i in range(len(df) - 3, 0, -1):
        candle = df.iloc[i]
        body_size = abs(candle["close"] - candle["open"]) / candle["open"]

        if body_size < MIN_BODY_PCT:
            continue

        next_candle = df.iloc[i + 1]

        # ── SHORT (Yeşil ana mum) ──
        if candle["close"] > candle["open"]:
            ref_high = candle["high"]
            ref_low  = candle["low"]

            # Hemen sonraki mum KIRMIZI ve high'ı geçiyor mu?
            if next_candle["close"] < next_candle["open"] and next_candle["high"] > ref_high:
                for k in range(i + 2, len(df)):
                    if df.iloc[k]["close"] < ref_low:
                        return "short", ref_low

        # ── LONG (Kırmızı ana mum) ──
        elif candle["close"] < candle["open"]:
            ref_low  = candle["low"]
            ref_high = candle["high"]

            # Hemen sonraki mum YEŞİL ve low'u geçiyor mu?
            if next_candle["close"] > next_candle["open"] and next_candle["low"] < ref_low:
                for k in range(i + 2, len(df)):
                    if df.iloc[k]["close"] > ref_high:
                        return "long", ref_high

    return None, None


# ─────────────────────────────────────────
#  BİLDİRİM MESAJI
# ─────────────────────────────────────────
def build_message(symbol, tf_label, signal, entry_price):
    direction   = "🟢 LONG"  if signal == "long"  else "🔴 SHORT"
    emoji_giris = "📈"       if signal == "long"  else "📉"

    return (
        f"{direction} SİNYALİ\n"
        f"{'─' * 30}\n"
        f"💎 Coin       : <b>{symbol}</b>\n"
        f"⏱ Timeframe  : <b>{tf_label}</b>\n"
        f"{emoji_giris} Giriş Seviyesi : <b>{entry_price}</b>\n"
        f"📌 Strateji   : Likidite Kapma + Yapı Kırılımı\n"
        f"{'─' * 30}\n"
        f"⚠️ Emir yönetimi size aittir."
    )


# ─────────────────────────────────────────
#  ANA TARAMA
# ─────────────────────────────────────────
def run_scan():
    log.info("=" * 50)
    log.info("🔍 TARAMA BAŞLADI")
    log.info("=" * 50)

    symbols = get_top_symbols(TOP_N)
    if not symbols:
        log.error("Coin listesi boş, tarama atlandı.")
        return

    found = 0

    for symbol in symbols:
        for tf_label, tf_interval in TIMEFRAMES.items():
            try:
                df = get_candles(symbol, tf_interval)
                signal, entry_price = detect_signal(df)
                key = f"{symbol}_{tf_label}"

                if signal and entry_price:
                    prev = sent_signals.get(key)
                    if prev and prev == (signal, round(entry_price, 6)):
                        continue

                    send_telegram(build_message(symbol, tf_label, signal, entry_price))
                    sent_signals[key] = (signal, round(entry_price, 6))
                    found += 1
                    log.info(f"✅ [{symbol}] [{tf_label}] {signal.upper()} | Giriş: {entry_price}")
                else:
                    if key in sent_signals:
                        del sent_signals[key]

                time.sleep(0.2)

            except Exception as e:
                log.error(f"[{symbol}] [{tf_label}] Hata: {e}")
                time.sleep(0.2)

    log.info(f"✅ Tarama tamamlandı. {found} sinyal bulundu.\n")


# ─────────────────────────────────────────
#  ANA DÖNGÜ
# ─────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("  LİKİDİTE KAPMA SİNYAL BOTU BAŞLADI")
    log.info(f"  Top {TOP_N} Coin | 1H / 2H / 4H")
    log.info(f"  Her {SCAN_EVERY // 60} dakikada bir tarama")
    log.info("=" * 50)

    send_telegram(
        "🤖 <b>Likidite Kapma Sinyal Botu Başladı</b>\n"
        f"📊 Top {TOP_N} coin taranıyor\n"
        f"⏱ Zaman Dilimleri: <b>1H / 2H / 4H</b>\n"
        f"🔄 Her {SCAN_EVERY // 60} dakikada bir tarama"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ana döngü hatası: {e}")

        log.info(f"⏳ {SCAN_EVERY // 60} dakika bekleniyor...")
        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    main()
