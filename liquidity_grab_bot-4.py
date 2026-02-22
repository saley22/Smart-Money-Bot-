import time
import logging
import requests
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger()

TELEGRAM_TOKEN   = "8407067459:AAGgGmH9jA6TwWHY-H62n6s9SKl3Bv0r1Mg"
TELEGRAM_CHAT_ID = "623705923"

TIMEFRAMES = {
    "1H":  "60",
    "2H":  "120",
    "4H":  "240",
}

MIN_BODY_PCT = 0.003
TOP_N        = 100
SCAN_EVERY   = 600
BYBIT_BASE   = "https://api.bybit.com"

sent_signals = {}


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code == 200:
            log.info("Telegram bildirimi gönderildi.")
        else:
            log.warning(f"Telegram hatası: {resp.text}")
    except Exception as e:
        log.error(f"Telegram hatası: {e}")


def get_top_symbols():
    try:
        resp = requests.get(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "linear"},
            timeout=10
        )
        data = resp.json()
        tickers = data["result"]["list"]
        usdt_pairs = [
            t for t in tickers
            if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
        ]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: float(x.get("turnover24h", 0)),
            reverse=True
        )
        symbols = [t["symbol"] for t in sorted_pairs[:TOP_N]]
        log.info(f"Top {TOP_N} coin alındı. İlk 5: {symbols[:5]}")
        return symbols
    except Exception as e:
        log.error(f"Sembol listesi alınamadı: {e}")
        return []


def get_candles(symbol, interval):
    try:
        resp = requests.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": 100
            },
            timeout=10
        )
        data = resp.json()
        raw = data["result"]["list"]
        raw = raw[::-1]  # Bybit en yeni mumu başa koyar, ters çevir

        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "turnover"
        ])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)

        df = df[:-1]  # Kapanmamış son mumu dahil etme
        return df.reset_index(drop=True)
    except Exception as e:
        log.error(f"[{symbol}] Mum verisi alınamadı: {e}")
        return pd.DataFrame()


def detect_signal(df):
    """
    LONG:
      - Kırmızı ana mum (close < open)
      - Sonraki mumlardan biri: low < ana low VE yeşil kapanmalı (close > open)  → likidite alındı
      - Daha sonra: close > ana high → LONG SİNYALİ, giriş = ana high

    SHORT:
      - Yeşil ana mum (close > open)
      - Sonraki mumlardan biri: high > ana high VE kırmızı kapanmalı (close < open) → likidite alındı
      - Daha sonra: close < ana low → SHORT SİNYALİ, giriş = ana low
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
                    if (df.iloc[j]["low"] < ref_low and
                            df.iloc[j]["close"] > df.iloc[j]["open"]):
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
                    if (df.iloc[j]["high"] > ref_high and
                            df.iloc[j]["close"] < df.iloc[j]["open"]):
                        liquidity_taken = True
                else:
                    if df.iloc[j]["close"] < ref_low:
                        return "short", ref_low

    return None, None


def build_message(symbol, tf_label, signal, entry_price):
    direction   = "🟢 LONG"  if signal == "long"  else "🔴 SHORT"
    emoji_giris = "📈"       if signal == "long"  else "📉"
    return (
        f"{direction} SİNYALİ\n"
        f"{'─' * 30}\n"
        f"💎 Coin          : <b>{symbol}</b>\n"
        f"⏱ Timeframe     : <b>{tf_label}</b>\n"
        f"{emoji_giris} Giriş Seviyesi : <b>{entry_price}</b>\n"
        f"📌 Strateji      : Likidite Kapma + Yapı Kırılımı\n"
        f"{'─' * 30}\n"
        f"⚠️ Emir yönetimi size aittir."
    )


def run_scan():
    log.info("TARAMA BAŞLADI")
    symbols = get_top_symbols()
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
                    if prev and prev == (signal, round(entry_price, 8)):
                        continue
                    send_telegram(build_message(symbol, tf_label, signal, entry_price))
                    sent_signals[key] = (signal, round(entry_price, 8))
                    found += 1
                    log.info(f"[{symbol}][{tf_label}] {signal.upper()} | Giriş: {entry_price}")
                else:
                    if key in sent_signals:
                        del sent_signals[key]

                time.sleep(0.2)

            except Exception as e:
                log.error(f"[{symbol}][{tf_label}] Hata: {e}")
                time.sleep(0.2)

    log.info(f"Tarama tamamlandı. {found} sinyal bulundu.")


def main():
    log.info("LİKİDİTE KAPMA SİNYAL BOTU BAŞLADI - BYBIT")
    log.info(f"Top {TOP_N} Coin | 1H / 2H / 4H | Her {SCAN_EVERY // 60} dakikada bir")

    send_telegram(
        "🤖 <b>Likidite Kapma Sinyal Botu Başladı</b>\n"
        f"📊 Bybit Top {TOP_N} coin taranıyor\n"
        f"⏱ Zaman Dilimleri: <b>1H / 2H / 4H</b>\n"
        f"🔄 Her {SCAN_EVERY // 60} dakikada bir tarama"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ana döngü hatası: {e}")
        log.info(f"{SCAN_EVERY // 60} dakika bekleniyor...")
        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    main()
