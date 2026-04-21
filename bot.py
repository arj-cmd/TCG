import asyncio
import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from scanner import run_scan
from broad_scanner import run_broad_scan
from watchlist import WATCHLIST
from ebay_buyer import execute_buy, get_item_details

load_dotenv()

TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID                 = os.getenv("TELEGRAM_CHAT_ID")
SCAN_INTERVAL_MINUTES   = int(os.getenv("SCAN_INTERVAL_MINUTES", "10"))
BROAD_SCAN_INTERVAL_MIN = int(os.getenv("BROAD_SCAN_INTERVAL_MINUTES", "60"))
THRESHOLD               = float(os.getenv("DISCOUNT_THRESHOLD", "0.82"))
BROAD_THRESHOLD         = float(os.getenv("BROAD_DISCOUNT_THRESHOLD", "0.80"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

already_alerted = set()

# Store pending purchases: {callback_data_key: deal_dict}
pending_purchases: dict = {}


# ─────────────────────────────────────────────────────────────
# MESSAGE FORMATTING
# ─────────────────────────────────────────────────────────────

def format_alert(deal: dict) -> str:
    discount    = deal["discount_pct"]
    emoji       = "🚨🚨" if discount >= 30 else "🚨"
    trend_map   = {"rising": "📈", "falling": "📉", "stable": "➡️", "unknown": "❓"}
    trend_emoji = trend_map.get(deal.get("trend", "unknown"), "❓")
    is_sealed   = deal.get("is_sealed", False)
    header_icon = "📦" if is_sealed else "🃏"
    type_label  = "SEALED PRODUCT" if is_sealed else "SINGLE CARD"
    scan_type   = "🌐 BROAD SCAN" if "broad" in deal.get("platform","").lower() else "📋 WATCHLIST"

    lines = [
        f"{emoji} *{discount}% BELOW MARKET — {type_label}* {emoji}",
        f"_{scan_type}_",
        f"",
        f"{header_icon} *Item:* {deal['card'][:60]}",
    ]
    if deal.get("card_code"):
        lines.append(f"🔢 *Code:* {deal['card_code']}")

    lines += [
        f"📊 *Grade:* {deal.get('grade', 'raw')}",
        f"🏪 *Platform:* {deal['platform']}",
        f"",
        f"💰 *Listed:* £{deal['listed']:.2f}",
        f"📈 *Market:* £{deal['market']:.2f}",
        f"💸 *Saving:* £{(deal['market'] - deal['listed']):.2f}",
        f"",
    ]

    if deal.get("trend") and deal["trend"] != "unknown":
        lines.append(f"{trend_emoji} *Trend:* {deal['trend'].capitalize()}")
        if deal.get("price_3m_ago"):
            lines.append(f"📅 *3 months ago:* £{deal['price_3m_ago']:.2f}")
        lines.append("")

    lines += [
        f"📝 {deal['title'][:80]}",
        f"",
        f"_Alert at {datetime.now().strftime('%H:%M')} UTC_",
    ]
    return "\n".join(lines)


def make_buy_keyboard(deal: dict) -> InlineKeyboardMarkup | None:
    """Only show Buy button for eBay listings (we can action those via API)."""
    url = deal.get("url", "")
    platform = deal.get("platform", "")

    # Buy button only for eBay
    is_ebay = "ebay" in platform.lower() or "ebay.co.uk" in url

    # Store deal for when user taps Buy
    deal_key = f"buy_{hash(url) % 999999}"
    pending_purchases[deal_key] = deal

    if is_ebay:
        keyboard = [
            [
                InlineKeyboardButton(f"✅ BUY £{deal['listed']:.2f}", callback_data=f"confirm_{deal_key}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"skip_{deal_key}"),
            ],
            [
                InlineKeyboardButton("🔗 View on eBay", url=url),
            ]
        ]
    else:
        # Non-eBay: just show view link, no buy button
        keyboard = [
            [
                InlineKeyboardButton("🔗 View Listing", url=url),
                InlineKeyboardButton("❌ Skip", callback_data=f"skip_{deal_key}"),
            ]
        ]

    return InlineKeyboardMarkup(keyboard)


# ─────────────────────────────────────────────────────────────
# CALLBACK HANDLERS (button taps)
# ─────────────────────────────────────────────────────────────

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    data     = query.data
    bot      = context.bot

    # ── Skip ──
    if data.startswith("skip_"):
        deal_key = data.replace("skip_", "buy_")  # normalise key
        pending_purchases.pop(deal_key, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            query.message.text + "\n\n_❌ Skipped_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ── Confirm intent (first tap on BUY) ──
    if data.startswith("confirm_"):
        deal_key = data.replace("confirm_", "buy_")
        deal = pending_purchases.get(deal_key)
        if not deal:
            await bot.send_message(chat_id=CHAT_ID, text="⚠️ Deal expired or already actioned.")
            return

        # Show confirmation message with final check
        confirm_text = (
            f"⚠️ *Confirm Purchase*\n\n"
            f"🃏 {deal['card'][:50]}\n"
            f"💰 £{deal['listed']:.2f}\n"
            f"📈 Market: £{deal['market']:.2f} ({deal['discount_pct']}% off)\n\n"
            f"_Tap Buy to execute on eBay_"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Buy", callback_data=f"execute_{deal_key}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{deal_key}"),
            ]
        ])
        await bot.send_message(
            chat_id=CHAT_ID,
            text=confirm_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        return

    # ── Execute purchase ──
    if data.startswith("execute_"):
        deal_key = data.replace("execute_", "buy_")
        deal = pending_purchases.get(deal_key)
        if not deal:
            await bot.send_message(chat_id=CHAT_ID, text="⚠️ Deal expired.")
            return

        await bot.send_message(chat_id=CHAT_ID, text="⏳ _Executing purchase..._",
                               parse_mode=ParseMode.MARKDOWN)

        # Verify item is still available at the price before buying
        item_details = get_item_details(deal["url"])
        if item_details:
            current_price = item_details["price"]
            if current_price > deal["listed"] * 1.05:  # price moved up >5%
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"⚠️ *Price changed!*\nWas: £{deal['listed']:.2f}\nNow: £{current_price:.2f}\n\n_Purchase cancelled — price moved._",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        result = execute_buy(deal["url"])

        if result["success"]:
            success_msg = (
                f"✅ *Purchase Complete!*\n\n"
                f"🃏 {result.get('title', deal['card'])[:60]}\n"
                f"💰 Total: £{result['total']:.2f}\n"
                f"🏪 Seller: {result.get('seller', 'unknown')}\n"
                f"📦 Order ID: `{result['order_id']}`\n\n"
                f"_Check your eBay account for delivery details_"
            )
            await bot.send_message(chat_id=CHAT_ID, text=success_msg,
                                   parse_mode=ParseMode.MARKDOWN)
            pending_purchases.pop(deal_key, None)
        else:
            error_msg = (
                f"❌ *Purchase Failed*\n\n"
                f"Reason: {result['error']}\n\n"
                f"_[View listing manually]({deal['url']})_"
            )
            await bot.send_message(chat_id=CHAT_ID, text=error_msg,
                                   parse_mode=ParseMode.MARKDOWN)
        return

    # ── Cancel ──
    if data.startswith("cancel_"):
        deal_key = data.replace("cancel_", "buy_")
        pending_purchases.pop(deal_key, None)
        await bot.send_message(chat_id=CHAT_ID, text="❌ Purchase cancelled.")
        return


# ─────────────────────────────────────────────────────────────
# SEND ALERT
# ─────────────────────────────────────────────────────────────

async def send_alert(bot: Bot, deal: dict):
    text     = format_alert(deal)
    keyboard = make_buy_keyboard(deal)
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )
        await asyncio.sleep(1)
    except Exception as e:
        log.error(f"Send alert error: {e}")


async def send_message(bot: Bot, text: str):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text,
                               parse_mode=ParseMode.MARKDOWN)
        await asyncio.sleep(0.5)
    except Exception as e:
        log.error(f"Send message error: {e}")


async def process_deals(bot: Bot, deals: list[dict]) -> int:
    new = 0
    for deal in deals:
        key = f"{deal['platform']}_{deal['url']}"
        if key in already_alerted:
            continue
        already_alerted.add(key)
        await send_alert(bot, deal)
        new += 1
    if len(already_alerted) > 1000:
        already_alerted.clear()
    return new


# ─────────────────────────────────────────────────────────────
# SCAN LOOPS
# ─────────────────────────────────────────────────────────────

async def watchlist_loop(bot: Bot):
    while True:
        log.info("📋 Watchlist scan starting...")
        try:
            deals = run_scan(WATCHLIST, threshold=THRESHOLD)
            new   = await process_deals(bot, deals)
            log.info(f"📋 Done — {new} new alerts")
        except Exception as e:
            log.error(f"Watchlist error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


async def broad_scan_loop(bot: Bot):
    await asyncio.sleep(BROAD_SCAN_INTERVAL_MIN * 60)
    while True:
        log.info("🌐 Broad scan starting...")
        try:
            await send_message(bot, "🌐 _Running broad market scan..._")
            deals = run_broad_scan(threshold=BROAD_THRESHOLD, pages=2)
            new   = await process_deals(bot, deals)
            log.info(f"🌐 Done — {new} new alerts")
            if new == 0:
                await send_message(bot, "🌐 _Broad scan complete — no new deals_")
        except Exception as e:
            log.error(f"Broad scan error: {e}")
        await asyncio.sleep(BROAD_SCAN_INTERVAL_MIN * 60)


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main():
    # Build Telegram application (needed for callback query handling)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_button))

    bot = app.bot
    me  = await bot.get_me()
    log.info(f"Bot started: @{me.username}")

    startup_msg = (
        f"✅ *OPTCG Bot Online — Buy Agent Active*\n\n"
        f"📋 Watchlist: *{len(WATCHLIST)}* cards every *{SCAN_INTERVAL_MINUTES} mins*\n"
        f"🌐 Broad scan: entire eBay market every *{BROAD_SCAN_INTERVAL_MIN} mins*\n"
        f"🎯 Watchlist threshold: *{round((1-THRESHOLD)*100)}%+* below market\n"
        f"🎯 Broad threshold: *{round((1-BROAD_THRESHOLD)*100)}%+* below market\n"
        f"🤖 Buy agent: *Semi-auto* \\(tap to confirm\\)\n\n"
        f"_eBay alerts include Buy/Skip buttons_\n"
        f"_Platforms: eBay · TCGPlayer · Beezie · Courtyard · Phygitals_"
    )
    await send_message(bot, startup_msg)

    # Run scan loops + Telegram polling concurrently
    await asyncio.gather(
        app.run_polling(allowed_updates=["callback_query"]),
        watchlist_loop(bot),
        broad_scan_loop(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())


