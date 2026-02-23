import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone

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

MIN_BODY_PCT  = 0.003
TOP_N         = 60
SCAN_EVERY    = 120
BINANCE_BASE  = "https://fapi.binance.com"

# Paper Trading Ayarları
BASLANGIC_BAKIYE  = 10000.0   # Başlangıç sanal bakiye ($)
ISLEM_BUYUKLUGU   = 500.0     # Her işlem için kullanılacak miktar ($)
KALDIRACH         = 2         # Kaldıraç
TP_PCT            = 0.08      # Take profit %8
SL_PCT            = 0.04      # Stop loss %4

sent_signals  = {}
acik_islemler = {}   # key: symbol_tf, value: {yon, giris, tp, sl, boyut}
bakiye        = BASLANGIC_BAKIYE
gunluk_kar    = 0.0
haftalik_kar  = 0.0
aylik_kar     = 0.0
toplam_islem  = 0
kazanan       = 0
kaybeden      = 0
son_gun       = datetime.now(timezone.utc).date()
son_hafta     = datetime.now(timezone.utc).isocalendar()[1]
son_ay        = datetime.now(timezone.utc).month


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.warning(f"Telegram hatası: {resp.text}")
    except Exception as e:
        log.error(f"Telegram hatası: {e}")


def get_top_symbols():
    try:
        resp = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=10)
        tickers = resp.json()
        usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT") and "_" not in t["symbol"]]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
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
            params={"symbol": symbol, "interval": interval, "limit": 100},
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
        df = df[:-1]
        return df.reset_index(drop=True)
    except Exception as e:
        log.error(f"[{symbol}] Mum verisi alınamadı: {e}")
        return pd.DataFrame()


def get_current_price(symbol):
    try:
        resp = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        return float(resp.json()["price"])
    except:
        return None


def detect_signal(df):
    if df.empty or len(df) < 3:
        return None, None

    son_mum = df.iloc[-1]
    baslangic = max(1, len(df) - 17)

    for i in range(len(df) - 3, baslangic - 1, -1):
        ana = df.iloc[i]
        body_size = abs(ana["close"] - ana["open"]) / ana["open"]
        if body_size < MIN_BODY_PCT:
            continue

        likit = df.iloc[i + 1]
        if (len(df) - 1) - (i + 1) > 15:
            continue

        ana_govde   = abs(ana["close"] - ana["open"])
        likit_govde = abs(likit["close"] - likit["open"])
        if likit_govde == 0 or ana_govde < likit_govde * 2:
            continue

        if ana["close"] < ana["open"]:
            ana_high, ana_low = ana["high"], ana["low"]
            ana_open, ana_close = ana["open"], ana["close"]
            likit_alindi = likit["low"] < ana_low
            govde_icinde = ana_close <= likit["close"] <= ana_open
            if not (likit_alindi and govde_icinde):
                continue
            sonraki_mumlar = df.iloc[i + 2: len(df) - 1]
            gecersiz = any(sonraki_mumlar.iloc[k]["close"] < ana_low for k in range(len(sonraki_mumlar)))
            if gecersiz:
                continue
            if son_mum["close"] > ana_high:
                return "long", ana_high

        elif ana["close"] > ana["open"]:
            ana_high, ana_low = ana["high"], ana["low"]
            ana_open, ana_close = ana["open"], ana["close"]
            likit_alindi = likit["high"] > ana_high
            govde_icinde = ana_open <= likit["close"] <= ana_close
            if not (likit_alindi and govde_icinde):
                continue
            sonraki_mumlar = df.iloc[i + 2: len(df) - 1]
            gecersiz = any(sonraki_mumlar.iloc[k]["close"] > ana_high for k in range(len(sonraki_mumlar)))
            if gecersiz:
                continue
            if son_mum["close"] < ana_low:
                return "short", ana_low

    return None, None


def islem_ac(key, symbol, tf_label, signal, entry_price):
    global bakiye, acik_islemler

    if bakiye < ISLEM_BUYUKLUGU:
        log.info(f"Yetersiz bakiye: {bakiye:.2f}$")
        return

    if key in acik_islemler:
        return

    pozisyon = ISLEM_BUYUKLUGU * KALDIRACH

    if signal == "long":
        tp = entry_price * (1 + TP_PCT)
        sl = entry_price * (1 - SL_PCT)
    else:
        tp = entry_price * (1 - TP_PCT)
        sl = entry_price * (1 + SL_PCT)

    bakiye -= ISLEM_BUYUKLUGU
    acik_islemler[key] = {
        "yon": signal,
        "giris": entry_price,
        "tp": tp,
        "sl": sl,
        "boyut": pozisyon,
        "symbol": symbol,
        "tf": tf_label,
        "zaman": datetime.now(timezone.utc).strftime("%H:%M")
    }

    yon_emoji = "🟢 LONG" if signal == "long" else "🔴 SHORT"
    msg = (
        f"{yon_emoji} SİNYALİ | LİKİDİTE KAPMA\n"
        f"{'─' * 30}\n"
        f"💎 Coin     : <b>{symbol}</b>\n"
        f"⏱ TF       : <b>{tf_label}</b>\n"
        f"📈 Giriş   : <b>{entry_price}</b>\n"
        f"🎯 TP      : <b>{tp:.4f}</b> (+%{TP_PCT*100:.0f})\n"
        f"🛑 SL      : <b>{sl:.4f}</b> (-%{SL_PCT*100:.0f})\n"
        f"💰 Pozisyon: <b>{pozisyon:.0f}$</b> ({KALDIRACH}x)\n"
        f"🏦 Bakiye  : <b>{bakiye:.2f}$</b>\n"
        f"{'─' * 30}\n"
        f"⚠️ Sanal işlem (Paper Trading)"
    )
    send_telegram(msg)
    log.info(f"[{symbol}][{tf_label}] {signal.upper()} işlem açıldı | Giriş: {entry_price}")


def islemleri_kontrol_et():
    global bakiye, acik_islemler, gunluk_kar, haftalik_kar, aylik_kar
    global toplam_islem, kazanan, kaybeden

    kapatilacaklar = []

    for key, islem in acik_islemler.items():
        fiyat = get_current_price(islem["symbol"])
        if not fiyat:
            continue

        kar = None
        sonuc = None

        if islem["yon"] == "long":
            if fiyat >= islem["tp"]:
                kar = islem["boyut"] * TP_PCT
                sonuc = "TP ✅"
                kazanan += 1
            elif fiyat <= islem["sl"]:
                kar = -islem["boyut"] * SL_PCT
                sonuc = "SL ❌"
                kaybeden += 1
        else:
            if fiyat <= islem["tp"]:
                kar = islem["boyut"] * TP_PCT
                sonuc = "TP ✅"
                kazanan += 1
            elif fiyat >= islem["sl"]:
                kar = -islem["boyut"] * SL_PCT
                sonuc = "SL ❌"
                kaybeden += 1

        if kar is not None:
            bakiye += ISLEM_BUYUKLUGU + kar
            gunluk_kar += kar
            haftalik_kar += kar
            aylik_kar += kar
            toplam_islem += 1
            kapatilacaklar.append(key)

            yon_emoji = "🟢 LONG" if islem["yon"] == "long" else "🔴 SHORT"
            kar_emoji = "💚" if kar > 0 else "❤️"
            msg = (
                f"{sonuc} İŞLEM KAPANDI | LİKİDİTE KAPMA\n"
                f"{'─' * 30}\n"
                f"💎 Coin     : <b>{islem['symbol']}</b>\n"
                f"⏱ TF       : <b>{islem['tf']}</b>\n"
                f"{yon_emoji} Yön      : <b>{islem['yon'].upper()}</b>\n"
                f"📈 Giriş   : <b>{islem['giris']}</b>\n"
                f"📉 Kapanış : <b>{fiyat}</b>\n"
                f"{kar_emoji} Kar/Zarar: <b>{kar:+.2f}$</b>\n"
                f"🏦 Bakiye  : <b>{bakiye:.2f}$</b>\n"
                f"{'─' * 30}"
            )
            send_telegram(msg)
            log.info(f"[{islem['symbol']}] {sonuc} | Kar: {kar:+.2f}$ | Bakiye: {bakiye:.2f}$")

    for key in kapatilacaklar:
        del acik_islemler[key]


def periyodik_ozet_kontrol():
    global son_gun, son_hafta, son_ay
    global gunluk_kar, haftalik_kar, aylik_kar

    simdi = datetime.now(timezone.utc)
    bugun = simdi.date()
    bu_hafta = simdi.isocalendar()[1]
    bu_ay = simdi.month

    # Günlük özet
    if bugun != son_gun:
        send_telegram(
            f"📊 <b>GÜNLÜK ÖZET | LİKİDİTE KAPMA</b>\n"
            f"{'─' * 30}\n"
            f"📅 Tarih        : <b>{son_gun}</b>\n"
            f"💰 Günlük K/Z   : <b>{gunluk_kar:+.2f}$</b>\n"
            f"🏦 Güncel Bakiye: <b>{bakiye:.2f}$</b>\n"
            f"📈 Toplam İşlem : <b>{toplam_islem}</b>\n"
            f"✅ Kazanan      : <b>{kazanan}</b>\n"
            f"❌ Kaybeden     : <b>{kaybeden}</b>\n"
            f"{'─' * 30}"
        )
        gunluk_kar = 0.0
        son_gun = bugun

    # Haftalık özet
    if bu_hafta != son_hafta:
        send_telegram(
            f"📊 <b>HAFTALIK ÖZET | LİKİDİTE KAPMA</b>\n"
            f"{'─' * 30}\n"
            f"💰 Haftalık K/Z : <b>{haftalik_kar:+.2f}$</b>\n"
            f"🏦 Güncel Bakiye: <b>{bakiye:.2f}$</b>\n"
            f"📈 Toplam İşlem : <b>{toplam_islem}</b>\n"
            f"✅ Kazanan      : <b>{kazanan}</b>\n"
            f"❌ Kaybeden     : <b>{kaybeden}</b>\n"
            f"{'─' * 30}"
        )
        haftalik_kar = 0.0
        son_hafta = bu_hafta

    # Aylık özet
    if bu_ay != son_ay:
        send_telegram(
            f"📊 <b>AYLIK ÖZET | LİKİDİTE KAPMA</b>\n"
            f"{'─' * 30}\n"
            f"💰 Aylık K/Z    : <b>{aylik_kar:+.2f}$</b>\n"
            f"🏦 Güncel Bakiye: <b>{bakiye:.2f}$</b>\n"
            f"📈 Toplam İşlem : <b>{toplam_islem}</b>\n"
            f"✅ Kazanan      : <b>{kazanan}</b>\n"
            f"❌ Kaybeden     : <b>{kaybeden}</b>\n"
            f"{'─' * 30}"
        )
        aylik_kar = 0.0
        son_ay = bu_ay


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
                    if not (prev and prev == signal):
                        islem_ac(key, symbol, tf_label, signal, entry_price)
                        sent_signals[key] = signal
                        found += 1

                time.sleep(0.2)

            except Exception as e:
                log.error(f"[{symbol}][{tf_label}] Hata: {e}")
                time.sleep(0.2)

    islemleri_kontrol_et()
    periyodik_ozet_kontrol()
    log.info(f"Tarama tamamlandı. {found} sinyal bulundu.")


def main():
    log.info("LİKİDİTE KAPMA PAPER TRADING BOTU BAŞLADI")
    send_telegram(
        f"🤖 <b>Likidite Kapma Paper Trading Botu Başladı</b>\n"
        f"{'─' * 30}\n"
        f"📊 Binance Top {TOP_N} coin taranıyor\n"
        f"⏱ Zaman Dilimleri: <b>1H / 2H / 4H</b>\n"
        f"💰 Başlangıç Bakiye: <b>{BASLANGIC_BAKIYE:.0f}$</b>\n"
        f"📈 İşlem Büyüklüğü: <b>{ISLEM_BUYUKLUGU:.0f}$ x {KALDIRACH}x</b>\n"
        f"🎯 TP: %{TP_PCT*100:.0f} | 🛑 SL: %{SL_PCT*100:.0f}\n"
        f"🔄 Her {SCAN_EVERY // 60} dakikada bir tarama"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ana döngü hatası: {e}")
        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    main()
