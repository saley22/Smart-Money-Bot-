"""
=============================================================
  LİKİDİTE KAPMA SİNYAL BOTU
  - Binance Futures Top 50 Hacim Tarayıcı
  - Zaman Dilimleri: 1H / 2H / 4H
  - Telegram Bildirim
  - Her 10 dakikada bir tarama
  - API KEY GEREKMİYOR
=============================================================

KURULUM:
  pip install requests pandas --user

ÇALIŞTIRMA:
  python liquidity_grab_bot.py
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

MIN_BODY_PCT = 0.003  # Ana mumun minimum gövde büyüklüğü (%0.3)
TOP_N        = 50     # Kaç coin taransın
SCAN_EVERY   = 600    # Kaç saniyede bir tarama (600 = 10 dakika)

BINANCE_BASE = "https://fapi.binance.com"

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

# Tekrar bildirim önleme
sent_signals = {}  # {symbol_tf: (signal, entry_price)}


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
#  EN HACİMLİ 50 COİNİ ÇEK (API KEY YOK)
# ─────────────────────────────────────────
def get_top_symbols(n=TOP_N):
    try:
        resp = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=10)
        tickers = resp.json()

        # Bazen Binance tek dict döndürür, liste olmasını garantile
        if isinstance(tickers, dict):
            tickers = [tickers]

        usdt_pairs = [
            t for t in tickers
            if isinstance(t, dict)
            and t.get("symbol", "").endswith("USDT")
            and "_" not in t.get("symbol", "")
        ]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: float(x.get("quoteVolume", 0)),
            reverse=True
        )
        symbols = [t["symbol"] for t in sorted_pairs[:n]]
        log.info(f"📊 Top {n} coin alındı. İlk 5: {symbols[:5]}")
        return symbols
    except Exception as e:
        log.error(f"Sembol listesi alınamadı: {e}")
        return []


# ─────────────────────────────────────────
#  MUM VERİSİ ÇEK (API KEY YOK)
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
    LONG:
      - Kırmızı ana mum (i)
      - Herhangi bir j > i: low < ana mumun low  → likidite alındı
      - Herhangi bir k > j: close > ana mumun high → SİNYAL
      - Limit giriş: ana mumun high

    SHORT:
      - Yeşil ana mum (i)
      - Herhangi bir j > i: high > ana mumun high → likidite alındı
      - Herhangi bir k > j: close < ana mumun low  → SİNYAL
      - Limit giriş: ana mumun low
    """
    if df.empty:
        return None, None

    for i in range(len(df) - 3, 0, -1):
        candle = df.iloc[i]
        body_size = abs(candle["close"] - candle["open"]) / candle["open"]

        if body_size < MIN_BODY_PCT:
            continue

        # ── LONG (Kırmızı ana mum) ──
        if candle["close"] < candle["open"]:
            ref_low  = candle["low"]
            ref_high = candle["high"]
            liquidity_taken = False

            for j in range(i + 1, len(df)):
                if not liquidity_taken:
                    if df.iloc[j]["low"] < ref_low:
                        liquidity_taken = True
                else:
                    if df.iloc[j]["close"] > ref_high:
                        return "long", ref_high

        # ── SHORT (Yeşil ana mum) ──
        elif candle["close"] > candle["open"]:
            ref_high = candle["high"]
            ref_low  = candle["low"]
            liquidity_taken = False

            for j in range(i + 1, len(df)):
                if not liquidity_taken:
                    if df.iloc[j]["high"] > ref_high:
                        liquidity_taken = True
                else:
                    if df.iloc[j]["close"] < ref_low:
                        return "short", ref_low

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
                        continue  # Aynı sinyal tekrar gönderilmesin

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
