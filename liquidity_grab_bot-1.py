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
