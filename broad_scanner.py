"""
broad_scanner.py

Broad market scan — searches ALL One Piece TCG listings on eBay,
extracts card/product identity from the title, looks up market price
on PriceCharting, and flags anything listed significantly below market.

Catches deals the watchlist would never find.
"""

import re, json, time, random, logging
from functools import lru_cache
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"},
]

# Map OP set codes → PriceCharting game slugs
SET_SLUG_MAP = {
    "OP01": "one-piece-romance-dawn",
    "OP02": "one-piece-paramount-war",
    "OP03": "one-piece-pillars-of-strength",
    "OP04": "one-piece-kingdoms-of-intrigue",
    "OP05": "one-piece-awakening-of-the-new-era",
    "OP06": "one-piece-wings-of-the-captain",
    "OP07": "one-piece-500-years-in-the-future",
    "OP08": "one-piece-two-legends",
    "OP09": "one-piece-emperors-in-the-new-world",
    "OP10": "one-piece-royal-blood",
    "OP11": "one-piece-a-fist-of-divine-speed",
    "OP12": "one-piece-legacy-of-the-master",
    "OP13": "one-piece-carrying-on-his-will",
    "ST01": "one-piece-starter-deck-straw-hat-crew",
    "ST02": "one-piece-starter-deck-worst-generation",
    "ST03": "one-piece-starter-deck-the-seven-warlords",
    "ST04": "one-piece-starter-deck-animal-kingdom-pirates",
    "ST05": "one-piece-starter-deck-film-edition",
    "ST06": "one-piece-starter-deck-absolute-justice",
    "ST07": "one-piece-starter-deck-big-mom-pirates",
    "ST08": "one-piece-starter-deck-monkey-d-luffy",
    "ST09": "one-piece-starter-deck-yamato",
    "ST10": "one-piece-starter-deck-royal-pirates-film-edition",
    "EB01": "one-piece-extra-booster-memorial-collection",
    "EB02": "one-piece-extra-booster-anime-25th-collection",
    "PRB01": "one-piece-premium-booster-the-best",
    "PRB02": "one-piece-premium-booster-the-best-vol-2",
    "P":    "one-piece-promo",
    "1ANN": "one-piece-english-version-1st-anniversary-set",
    "25ANN": "one-piece-25th-anniversary-premium-card-collection",
}

# Sealed product keywords → PriceCharting product slugs
SEALED_KEYWORDS = {
    "booster box":    "booster-box",
    "sealed box":     "booster-box",
    "case":           "booster-case",
    "booster pack":   "booster-pack",
    "starter deck":   "starter-deck",
    "premium booster":"premium-booster-box",
}

# Price cache to avoid re-querying PriceCharting for same card repeatedly
_price_cache: dict = {}


def get_headers():
    return random.choice(HEADERS_POOL)


def polite_get(url, params=None, timeout=15):
    time.sleep(random.uniform(1.0, 2.5))
    try:
        return httpx.get(url, headers=get_headers(), params=params,
                         follow_redirects=True, timeout=timeout)
    except Exception as e:
        log.warning(f"Request failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# TITLE PARSER
# Extracts card identity from messy eBay listing titles
# ─────────────────────────────────────────────────────────────

def parse_listing_title(title: str) -> dict:
    """
    From an eBay title like:
      "ONE PIECE TCG Monkey D Luffy OP05-119 Manga Rare PSA 10 Gem Mint"
      "One Piece Booster Box OP01 Romance Dawn Sealed English"
      "One Piece Zoro ST01-012 Secret Rare Alt Art NM"

    Extracts:
      - set_code: "OP05"
      - card_code: "OP05-119"
      - grade: "PSA 10" / "raw"
      - is_sealed: True/False
      - sealed_type: "booster-box" etc
      - search_name: cleaned name for PriceCharting lookup
    """
    t = title.upper()
    result = {
        "original": title,
        "set_code": None,
        "card_code": None,
        "grade": "raw",
        "is_sealed": False,
        "sealed_type": None,
        "search_name": None,
        "pc_game_slug": None,
    }

    # ── Detect sealed products first ──
    title_lower = title.lower()
    for keyword, slug in SEALED_KEYWORDS.items():
        if keyword in title_lower:
            result["is_sealed"] = True
            result["sealed_type"] = slug
            break

    # ── Extract grading info ──
    grade_patterns = [
        (r"PSA\s*10", "PSA 10"),
        (r"PSA\s*9\.5", "PSA 9.5"),
        (r"PSA\s*9\b", "PSA 9"),
        (r"PSA\s*8\b", "PSA 8"),
        (r"BGS\s*10", "BGS 10"),
        (r"BGS\s*9\.5", "BGS 9.5"),
        (r"BGS\s*9\b", "BGS 9"),
        (r"CGC\s*10", "CGC 10"),
        (r"CGC\s*9\.5", "CGC 9.5"),
        (r"GEM\s*MINT", "PSA 10"),
        (r"BLACK\s*LABEL", "BGS Black Label"),
    ]
    for pattern, grade_label in grade_patterns:
        if re.search(pattern, t):
            result["grade"] = grade_label
            break

    # ── Extract set/card code (e.g. OP05-119, ST01-012, P-001) ──
    code_match = re.search(r"\b(OP\d{2}|ST\d{2}|EB\d{2}|PRB\d{2}|P)-(\d{3,4})\b", t)
    if code_match:
        result["card_code"] = code_match.group(0)
        result["set_code"]  = code_match.group(1)
        result["pc_game_slug"] = SET_SLUG_MAP.get(result["set_code"], "one-piece-promo")

    # ── Extract set code alone if no full card code ──
    if not result["set_code"]:
        set_match = re.search(r"\b(OP\d{2}|ST\d{2}|EB\d{2}|PRB\d{2})\b", t)
        if set_match:
            result["set_code"] = set_match.group(1)
            result["pc_game_slug"] = SET_SLUG_MAP.get(result["set_code"])

    # ── Build search name ──
    # Strip junk words, keep card name + code
    clean = re.sub(
        r"(ONE PIECE|OPTCG|TCG|CARD GAME|TRADING CARD|JAPANESE|ENGLISH|"
        r"NM|NEAR MINT|MINT|GEM|PSA|BGS|CGC|GRADED|SLAB|SEALED|"
        r"BOOSTER BOX|BOOSTER PACK|STARTER DECK|CASE|NEW|"
        r"FREE SHIP|FREE P&P|FAST|UK SELLER|\bOP\d{2}\b|\bST\d{2}\b|"
        r"GRADE \d+|\d+/\d+|\bLOT\b|\bBUNDLE\b)",
        "", t
    ).strip()
    clean = re.sub(r"\s+", " ", clean)
    result["search_name"] = clean[:60]

    return result


# ─────────────────────────────────────────────────────────────
# PRICECHARTING LOOKUP (with cache)
# ─────────────────────────────────────────────────────────────

def lookup_pricecharting_price(parsed: dict) -> float | None:
    """
    Looks up market price on PriceCharting using:
    1. Card code (e.g. OP05-119) → most accurate
    2. Search name → fallback
    Returns price in GBP.
    """
    cache_key = f"{parsed.get('card_code') or parsed.get('search_name')}_{parsed['grade']}"
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    price = None

    # ── Try card code search first ──
    if parsed.get("card_code") and parsed.get("pc_game_slug"):
        slug = parsed["card_code"].lower().replace(" ", "-")
        url = f"https://www.pricecharting.com/game/{parsed['pc_game_slug']}/{slug}"
        price = _scrape_pc_price(url, parsed["grade"])

    # ── Fallback: PriceCharting text search ──
    if not price and parsed.get("search_name"):
        search_url = "https://www.pricecharting.com/search-products"
        resp = polite_get(search_url, params={"q": f"one piece {parsed['search_name']}", "type": "prices"})
        if resp and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            first_result = soup.select_one("table.games td.title a, #games td.title a")
            if first_result:
                href = first_result.get("href", "")
                if href:
                    full_url = f"https://www.pricecharting.com{href}"
                    price = _scrape_pc_price(full_url, parsed["grade"])

    if price:
        _price_cache[cache_key] = price
        log.info(f"  PC price [{parsed['grade']}]: £{price:.2f} — {parsed.get('card_code') or parsed.get('search_name')}")

    return price


def _scrape_pc_price(url: str, grade: str) -> float | None:
    """Scrape a specific PriceCharting product page for the price matching the grade."""
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    grade_map = {
        "raw":            ["Ungraded", "ungraded", "Complete"],
        "psa 10":         ["PSA 10", "Grade 10"],
        "psa 9.5":        ["PSA 9.5"],
        "psa 9":          ["PSA 9", "Grade 9"],
        "psa 8":          ["PSA 8"],
        "bgs 10":         ["BGS 10", "Beckett 10"],
        "bgs 9.5":        ["BGS 9.5"],
        "bgs black label":["BGS Black Label", "Black Label"],
        "cgc 10":         ["CGC 10"],
        "cgc 9.5":        ["CGC 9.5"],
    }

    candidates = grade_map.get(grade.lower(), [grade])

    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        row_label = cells[0].get_text(strip=True)
        for label in candidates:
            if label.lower() in row_label.lower():
                raw = cells[-1].get_text(strip=True)
                raw = raw.replace("£","").replace("$","").replace(",","").strip()
                try:
                    usd = float(raw)
                    return round(usd * 0.79, 2)
                except ValueError:
                    pass

    # If no grade match, return ungraded/first price as fallback
    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) >= 2:
            raw = cells[-1].get_text(strip=True)
            raw = raw.replace("£","").replace("$","").replace(",","").strip()
            try:
                val = float(raw)
                if val > 0:
                    return round(val * 0.79, 2)
            except ValueError:
                pass

    return None


# ─────────────────────────────────────────────────────────────
# BROAD EBAY SCAN
# ─────────────────────────────────────────────────────────────

def broad_ebay_scan(pages: int = 3) -> list[dict]:
    """
    Fetches multiple pages of One Piece TCG listings from eBay UK.
    Returns raw listing dicts with title + price + url.
    `pages` = number of result pages to fetch (60 listings per page).
    """
    all_listings = []
    search_queries = [
        # Broad singles
        "One Piece TCG card English",
        "One Piece TCG PSA graded English",
        "One Piece TCG manga rare English",
        "One Piece TCG promo English",
        "One Piece TCG flagship battle",
        "One Piece TCG championship English",

        # Sealed product — booster boxes
        "One Piece card game booster box sealed English",
        "One Piece TCG booster box OP01 English",
        "One Piece TCG booster box OP02 English",
        "One Piece TCG booster box OP03 English",
        "One Piece TCG booster box OP04 English",
        "One Piece TCG booster box OP05 English",
        "One Piece TCG booster box OP06 English",
        "One Piece TCG booster box OP07 English",
        "One Piece TCG booster box OP08 English",

        # Special sets
        "One Piece 1st Anniversary Set English",
        "One Piece 25th Anniversary Premium Collection English",
        "One Piece Card The Best PRB English",
        "One Piece Memorial Collection EB01 English sealed",
    ]

    for query in search_queries:
        for page in range(1, pages + 1):
            params = {
                "_nkw":    query,
                "_sacat":  "2536",      # Trading Cards
                "LH_BIN":  "1",         # Buy It Now
                "_sop":    "10",        # Newly listed
                "_ipg":    "60",
                "_pgn":    str(page),
                "LH_PrefLoc": "1",      # UK preferred
            }
            resp = polite_get("https://www.ebay.co.uk/sch/i.html", params=params)
            if not resp or resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            page_count = 0

            for item in soup.select(".s-item"):
                title_el = item.select_one(".s-item__title")
                price_el = item.select_one(".s-item__price")
                link_el  = item.select_one(".s-item__link")

                if not (title_el and price_el and link_el):
                    continue
                title = title_el.text.strip()
                if title.lower() in ("shop on ebay", ""):
                    continue

                price_text = price_el.text.strip().split(" to ")[0]
                m = re.search(r"[\d,]+\.?\d*", price_text.replace(",",""))
                if not m:
                    continue
                try:
                    price = float(m.group())
                except ValueError:
                    continue

                # Skip very cheap listings (likely singles not worth scanning)
                # and very expensive ones that are clearly lots/bundles
                if price < 5 or price > 15000:
                    continue

                all_listings.append({
                    "title": title,
                    "price": price,
                    "url": link_el["href"].split("?")[0],
                })
                page_count += 1

            log.info(f"  Broad eBay [{query[:30]}] page {page}: {page_count} listings")

            # Stop if last page returned no results
            if page_count == 0:
                break

    # Deduplicate by URL
    seen = set()
    unique = []
    for item in all_listings:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)

    log.info(f"  Broad eBay total unique listings: {len(unique)}")
    return unique


# ─────────────────────────────────────────────────────────────
# MAIN BROAD SCAN
# ─────────────────────────────────────────────────────────────

def run_broad_scan(threshold: float = 0.80, pages: int = 2) -> list[dict]:
    """
    Full broad market scan:
    1. Fetch all recent One Piece eBay listings
    2. Parse each title to identify the card/product
    3. Look up market price on PriceCharting
    4. Return anything listed below threshold x market price

    threshold: alert when listed below this fraction of market price
               0.80 = 20%+ below market (recommended for broad scan)
    pages:     eBay result pages per query (2 = ~120 listings per query)
    """
    log.info("🌐 Starting broad market scan...")
    deals = []

    listings = broad_ebay_scan(pages=pages)
    log.info(f"  Scanning {len(listings)} listings against PriceCharting...")

    for i, listing in enumerate(listings):
        if i % 20 == 0:
            log.info(f"  Progress: {i}/{len(listings)}")

        parsed = parse_listing_title(listing["title"])

        # Skip if we can't identify the card well enough
        if not parsed.get("card_code") and not parsed.get("search_name"):
            continue

        # Skip very generic titles we can't match
        if parsed.get("search_name") and len(parsed["search_name"]) < 8:
            continue

        market_price = lookup_pricecharting_price(parsed)
        if not market_price or market_price < 5:
            continue

        ratio = listing["price"] / market_price
        if ratio < threshold:
            discount_pct = round((1 - ratio) * 100)

            # Extra context for sealed products
            product_type = "📦 SEALED" if parsed["is_sealed"] else "🃏 SINGLE"

            deals.append({
                "card":         listing["title"][:60],
                "grade":        parsed["grade"],
                "platform":     "eBay (broad)",
                "listed":       listing["price"],
                "market":       market_price,
                "discount_pct": discount_pct,
                "url":          listing["url"],
                "title":        listing["title"],
                "trend":        "unknown",
                "price_3m_ago": None,
                "product_type": product_type,
                "card_code":    parsed.get("card_code", ""),
                "is_sealed":    parsed["is_sealed"],
            })
            log.info(f"  🚨 Deal: {listing['title'][:50]} — {discount_pct}% off (£{listing['price']} vs £{market_price})")

    # Clear price cache after broad scan to keep memory clean
    _price_cache.clear()

    log.info(f"  Broad scan complete — {len(deals)} deals found")
    return sorted(deals, key=lambda x: x["discount_pct"], reverse=True)
