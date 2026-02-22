import os
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

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAMES = {
    "1H": "1h",
    "2H": "2h",
    "4H": "4h",
}

MIN_BODY_PCT  = 0.003   # Ana mumun minimum gövde büyüklüğü (%)
TOP_N         = 60      # Taranacak coin sayısı (en hacimli 60)
SCAN_EVERY    = 120     # Tarama sıklığı (saniye) - 2 dakika
BINANCE_BASE  = "https://fapi.binance.com"

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
            f"{BINANCE_BASE}/fapi/v1/ticker/24hr",
            timeout=10
        )
        tickers = resp.json()
        usdt_pairs = [
            t for t in tickers
            if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
        ]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: float(x.get("quoteVolume", 0)),
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
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": 100
            },
            timeout=10
        )
        raw = resp.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close",
            "volume", "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
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
    ── LONG SİNYALİ ──
    1. Ana mum: KIRMIZI (close < open), yeterli gövde büyüklüğüne sahip
    2. Hemen sonraki İLK mum (likidite mumu):
       - Ana mumun low'unu kırmalı (low < ana_low)  → likidite aldı
       - Gövdesiyle ana mumun gövdesi içinde kapanmalı (ana_close <= close <= ana_open)
    3. Likidite mumundan sonraki mumlardan herhangi biri:
       - Ana mumun high'ının ÜSTÜNDE kapanırsa (close > ana_high) → LONG SİNYALİ
       - Giriş = ana mumun high'ı

    ── SHORT SİNYALİ ──
    1. Ana mum: YEŞİL (close > open), yeterli gövde büyüklüğüne sahip
    2. Hemen sonraki İLK mum (likidite mumu):
       - Ana mumun high'ını kırmalı (high > ana_high)  → likidite aldı
       - Gövdesiyle ana mumun gövdesi içinde kapanmalı (ana_open <= close <= ana_close)
    3. Likidite mumundan sonraki mumlardan herhangi biri:
       - Ana mumun low'unun ALTINDA kapanırsa (close < ana_low) → SHORT SİNYALİ
       - Giriş = ana mumun low'u
    """
    if df.empty:
        return None, None

    # En az 3 mum gerekli: ana mum + likidite mumu + kırılım mumu
    for i in range(len(df) - 3, 0, -1):
        ana = df.iloc[i]

        # Ana mumun gövde büyüklüğü kontrolü
        body_size = abs(ana["close"] - ana["open"]) / ana["open"]
        if body_size < MIN_BODY_PCT:
            continue

        # Sonraki mum var mı kontrol et
        if i + 2 >= len(df):
            continue

        likit = df.iloc[i + 1]  # Likidite mumu (ana mumdan hemen sonraki İLK mum)

        # ── LONG (Kırmızı ana mum) ──
        if ana["close"] < ana["open"]:
            ana_high  = ana["high"]
            ana_low   = ana["low"]
            ana_open  = ana["open"]   # Kırmızı mumda open üstte
            ana_close = ana["close"]  # Kırmızı mumda close altta

            # Şart 1: Likidite mumu ana mumun low'unu kırmalı
            likit_alindi = likit["low"] < ana_low

            # Şart 2: Likidite mumunun close'u ana mumun gövdesi içinde olmalı
            # Kırmızı mum gövdesi: ana_close (alt) ile ana_open (üst) arasında
            gövde_içinde = ana_close <= likit["close"] <= ana_open

            if likit_alindi and gövde_içinde:
                # Şart 3: Sonraki mumlardan biri ana high'ın üstünde kapanmalı
                for j in range(i + 2, len(df)):
                    if df.iloc[j]["close"] > ana_high:
                        return "long", ana_high

        # ── SHORT (Yeşil ana mum) ──
        elif ana["close"] > ana["open"]:
            ana_high  = ana["high"]
            ana_low   = ana["low"]
            ana_open  = ana["open"]   # Yeşil mumda open altta
            ana_close = ana["close"]  # Yeşil mumda close üstte

            # Şart 1: Likidite mumu ana mumun high'ını kırmalı
            likit_alindi = likit["high"] > ana_high

            # Şart 2: Likidite mumunun close'u ana mumun gövdesi içinde olmalı
            # Yeşil mum gövdesi: ana_open (alt) ile ana_close (üst) arasında
            gövde_içinde = ana_open <= likit["close"] <= ana_close

            if likit_alindi and gövde_içinde:
                # Şart 3: Sonraki mumlardan biri ana low'un altında kapanmalı
                for j in range(i + 2, len(df)):
                    if df.iloc[j]["close"] < ana_low:
                        return "short", ana_low

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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID environment variable olarak tanımlanmalı!")

    log.info("LİKİDİTE KAPMA SİNYAL BOTU BAŞLADI - BİNANCE")
    log.info(f"Top {TOP_N} Coin | 1H / 2H / 4H | Her {SCAN_EVERY // 60} dakikada bir")

    send_telegram(
        "🤖 <b>Likidite Kapma Sinyal Botu Başladı</b>\n"
        f"📊 Binance Top {TOP_N} coin taranıyor\n"
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
