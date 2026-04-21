import asyncio
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from scanner import run_scan
from watchlist import WATCHLIST

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
THRESHOLD = float(os.getenv("DISCOUNT_THRESHOLD", "0.82"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

already_alerted = set()


def format_alert(deal: dict) -> str:
    discount = deal["discount_pct"]
    emoji = "🚨🚨" if discount >= 30 else "🚨"
    trend_map = {"rising": "📈", "falling": "📉", "stable": "➡️", "unknown": "❓"}
    trend_emoji = trend_map.get(deal.get("trend", "unknown"), "❓")

    lines = [
        f"{emoji} *{discount}% BELOW MARKET* {emoji}",
        "",
        f"🃏 *Card:* {deal['card']}",
        f"📊 *Grade:* {deal.get('grade', 'raw')}",
        f"🏪 *Platform:* {deal['platform']}",
        "",
        f"💰 *Listed:* £{deal['listed']:.2f}",
        f"📈 *Market:* £{deal['market']:.2f}",
        f"💸 *Saving:* £{(deal['market'] - deal['listed']):.2f}",
        "",
    ]

    if deal.get("trend") and deal["trend"] != "unknown":
        lines.append(f"{trend_emoji} *Price trend:* {deal['trend'].capitalize()}")
        if deal.get("price_3m_ago"):
            lines.append(f"📅 *3 months ago:* £{deal['price_3m_ago']:.2f}")
        lines.append("")

    lines += [
        f"📝 {deal['title'][:80]}",
        "",
        f"🔗 [View Listing]({deal['url']})",
        "",
        f"_Alert at {datetime.now().strftime('%H:%M')} UTC_",
    ]
    return "\n".join(lines)


async def send_startup_message(bot: Bot):
    msg = (
        f"✅ *OPTCG Bot Online*\n\n"
        f"📋 Watching *{len(WATCHLIST)}* cards\n"
        f"⏱️ Scanning every *{SCAN_INTERVAL_MINUTES} minutes*\n"
        f"🎯 Alerting when *{round((1-THRESHOLD)*100)}%+* below market\n\n"
        f"_Platforms: eBay · TCGPlayer · Beezie · Courtyard · Phygitals_\n"
        f"_Price source: PriceCharting \\+ eBay sold_"
    )
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)


async def scan_loop(bot: Bot):
    while True:
        log.info("🔍 Starting scan...")
        try:
            deals = run_scan(WATCHLIST, threshold=THRESHOLD)
            log.info(f"Scan complete — {len(deals)} deals found")
        except Exception as e:
            log.error(f"Scan error: {e}")
            await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)
            continue

        new_deals = 0
        for deal in deals:
            key = f"{deal['platform']}_{deal['url']}"
            if key in already_alerted:
                continue
            already_alerted.add(key)
            try:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_alert(deal),
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False,
                )
                new_deals += 1
                log.info(f"✅ Alerted: {deal['card']} — {deal['discount_pct']}% off on {deal['platform']}")
                await asyncio.sleep(1)
            except Exception as e:
                log.error(f"Failed to send alert: {e}")

        if new_deals == 0:
            log.info("No new deals this scan.")
        if len(already_alerted) > 500:
            already_alerted.clear()

        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    me = await bot.get_me()
    log.info(f"Bot started: @{me.username}")
    await send_startup_message(bot)
    await scan_loop(bot)


if __name__ == "__main__":
    asyncio.run(main())

