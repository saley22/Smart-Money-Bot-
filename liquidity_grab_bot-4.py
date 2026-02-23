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
    LONG:
    1. Kırmızı ana mum
    2. Hemen sonraki ilk mum: low'unu kırar + ana mumun gövdesi içinde kapanır
    3. Ana mumun gövdesi, likidite mumunun gövdesinden en az 2 kat büyük olmalı
    4. Likidite sonrası hiçbir mum ana mumun ALTINDA kapanmamalı
    5. Likidite sonrası en fazla 15 mum içinde kırılım olmalı
    6. En son kapanan mum ana mumun HIGH'ının üstünde kaparsa → LONG sinyali

    SHORT:
    1. Yeşil ana mum
    2. Hemen sonraki ilk mum: high'ını kırar + ana mumun gövdesi içinde kapanır
    3. Ana mumun gövdesi, likidite mumunun gövdesinden en az 2 kat büyük olmalı
    4. Likidite sonrası hiçbir mum ana mumun ÜSTÜNDE kapanmamalı
    5. Likidite sonrası en fazla 15 mum içinde kırılım olmalı
    6. En son kapanan mum ana mumun LOW'unun altında kaparsa → SHORT sinyali
    """
    if df.empty or len(df) < 3:
        return None, None

    son_mum = df.iloc[-1]  # En son kapanan mum

    # Geriye doğru sadece 15 mum içinde ara
    baslangic = max(1, len(df) - 17)

    for i in range(len(df) - 3, baslangic - 1, -1):
        ana = df.iloc[i]

        # Ana mumun gövde büyüklüğü kontrolü
        body_size = abs(ana["close"] - ana["open"]) / ana["open"]
        if body_size < MIN_BODY_PCT:
            continue

        likit = df.iloc[i + 1]  # Likidite mumu

        # Likidite ile kırılım arasında en fazla 15 mum olmalı
        if (len(df) - 1) - (i + 1) > 15:
            continue

        # Ana mumun ve likidite mumunun gövde büyüklükleri
        ana_govde   = abs(ana["close"] - ana["open"])
        likit_govde = abs(likit["close"] - likit["open"])

        # Ana mumun gövdesi likidite mumunun en az 2 katı olmalı
        if likit_govde == 0 or ana_govde < likit_govde * 2:
            continue

        # ── LONG (Kırmızı ana mum) ──
        if ana["close"] < ana["open"]:
            ana_high  = ana["high"]
            ana_low   = ana["low"]
            ana_open  = ana["open"]
            ana_close = ana["close"]

            likit_alindi = likit["low"] < ana_low
            govde_icinde = ana_close <= likit["close"] <= ana_open

            if not (likit_alindi and govde_icinde):
                continue

            # Likidite sonrası hiçbir mum ana low'un altında kapanmamalı
            sonraki_mumlar = df.iloc[i + 2: len(df) - 1]
            gecersiz = any(sonraki_mumlar.iloc[k]["close"] < ana_low for k in range(len(sonraki_mumlar)))
            if gecersiz:
                continue

            if son_mum["close"] > ana_high:
                return "long", ana_high

        # ── SHORT (Yeşil ana mum) ──
        elif ana["close"] > ana["open"]:
            ana_high  = ana["high"]
            ana_low   = ana["low"]
            ana_open  = ana["open"]
            ana_close = ana["close"]

            likit_alindi = likit["high"] > ana_high
            govde_icinde = ana_open <= likit["close"] <= ana_close

            if not (likit_alindi and govde_icinde):
                continue

            # Likidite sonrası hiçbir mum ana high'ın üstünde kapanmamalı
            sonraki_mumlar = df.iloc[i + 2: len(df) - 1]
            gecersiz = any(sonraki_mumlar.iloc[k]["close"] > ana_high for k in range(len(sonraki_mumlar)))
            if gecersiz:
                continue

            if son_mum["close"] < ana_low:
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
                    if prev and prev == signal:
                        continue
                    send_telegram(build_message(symbol, tf_label, signal, entry_price))
                    sent_signals[key] = signal
                    found += 1
                    log.info(f"[{symbol}][{tf_label}] {signal.upper()} | Giriş: {entry_price}")

                time.sleep(0.2)

            except Exception as e:
                log.error(f"[{symbol}][{tf_label}] Hata: {e}")
                time.sleep(0.2)

    log.info(f"Tarama tamamlandı. {found} sinyal bulundu.")


def main():
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
