import os, re, time, random, logging
import requests, httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

EBAY_APP_ID    = os.getenv("EBAY_APP_ID", "")
TCGAPI_DEV_KEY = os.getenv("TCGAPI_DEV_KEY", "")

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"},
]

# Price cache — avoids re-fetching same card within 2 hours

def get_headers():
    return random.choice(HEADERS_POOL)

def polite_get(url, params=None, timeout=15):
    time.sleep(random.uniform(1.0, 2.0))
    try:
        return httpx.get(url, headers=get_headers(), params=params,
                         follow_redirects=True, timeout=timeout)
    except Exception as e:
        log.warning(f"Request failed {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# TCGAPI.DEV — PRIMARY PRICE SOURCE
# Free 100 req/day — sign up at tcgapi.dev
# Header: X-API-Key (not Authorization Bearer)
# ─────────────────────────────────────────────────────────────

def get_tcgapi_price(card_name: str, grade: str = "raw") -> float | None:
    """
    Look up market price from tcgapi.dev.
    Results cached for 2 hours to preserve the 100 req/day free quota.
    """
    if not TCGAPI_DEV_KEY:
        log.warning("  TCGAPI_DEV_KEY not set — no price source available")
        return None

    # Strip noise words — keep character name + set code for best tcgapi match
    query = card_name.lower()
    query = re.sub(r"\b(english|sealed|raw|psa \d+\.?\d*|bgs \d+\.?\d*|cgc \d+\.?\d*)\b", "", query)
    query = re.sub(r"\b(manga rare|1st anniversary set|2nd anniversary set|3rd anniversary set|flagship battle|championship|trophy|regional winner|super pre-release winner|wanted poster|misprint|alt art|serial number)\b", "", query)
    query = re.sub(r"\s+", " ", query).strip()

    try:
        resp = requests.get(
            "https://api.tcgapi.dev/v1/search",
            headers={"X-API-Key": TCGAPI_DEV_KEY},
            params={"q": query, "game": "one-piece"},
            timeout=10
        )

        if resp.status_code == 403:
            log.warning(f"  tcgapi.dev: 403 — check API key is valid")
            return None
        if resp.status_code != 200:
            log.warning(f"  tcgapi.dev: {resp.status_code} — {resp.text[:100]}")
            return None

        data  = resp.json()
        cards = data.get("data", [])
        if not cards:
            log.info(f"  tcgapi.dev: no results for '{query}'")
            return None

        card  = cards[0]

        # Pick the right price field based on grade
        grade_lower = grade.lower()
        if "psa 10" in grade_lower or "cgc 10" in grade_lower:
            raw_price = card.get("price")
        elif "psa 9" in grade_lower:
            raw_price = card.get("low_price") or card.get("price")
        elif "sealed" in grade_lower:
            raw_price = card.get("price")
        else:
            raw_price = card.get("low_price") or card.get("price")

        if not raw_price:
            log.info(f"  tcgapi.dev: no price field for '{query}'")
            return None

        # tcgapi.dev returns USD — convert to GBP
        gbp = round(float(raw_price) * 0.79, 2)
        log.info(f"  tcgapi.dev: £{gbp:.2f} for '{query}' [{grade}]")
        return gbp

    except Exception as e:
        log.warning(f"  tcgapi.dev error: {e}")
        return None


def get_market_price(card_name: str, grade: str) -> float | None:
    """
    Get market price — tcgapi.dev first, eBay API fallback when approved.
    """
    # 1. tcgapi.dev
    price = get_tcgapi_price(card_name, grade)
    if price and price > 1:
        return price

    # 2. eBay Finding API (when approved — add EBAY_APP_ID to Railway)
    if EBAY_APP_ID:
        price = get_ebay_sold_avg_api(card_name, grade)
        if price:
            return price

    log.warning(f"  No market price found for '{card_name}' [{grade}]")
    return None


# ─────────────────────────────────────────────────────────────
# EBAY FINDING API — listing search + sold price fallback
# Works when EBAY_APP_ID is set (approved tomorrow)
# ─────────────────────────────────────────────────────────────

def get_ebay_sold_avg_api(card_name: str, grade: str = "raw") -> float | None:
    if not EBAY_APP_ID:
        return None
    grade_str = "" if grade in ("raw", "sealed") else grade
    query     = f"One Piece {card_name} {grade_str}".strip()
    params = {
        "OPERATION-NAME":                 "findCompletedItems",
        "SERVICE-VERSION":                "1.0.0",
        "SECURITY-APPNAME":               EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT":           "JSON",
        "keywords":                       query,
        "categoryId":                     "2536",
        "itemFilter(0).name":             "SoldItemsOnly",
        "itemFilter(0).value":            "true",
        "itemFilter(1).name":             "Currency",
        "itemFilter(1).value":            "GBP",
        "sortOrder":                      "EndTimeSoonest",
        "paginationInput.entriesPerPage": "20",
    }
    try:
        resp   = requests.get("https://svcs.ebay.com/services/search/FindingService/v1",
                              params=params, timeout=15)
        items  = (resp.json().get("findCompletedItemsResponse", [{}])[0]
                             .get("searchResult", [{}])[0]
                             .get("item", []))
        prices = []
        for item in items:
            try:
                prices.append(float(item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"]))
            except:
                pass
        if not prices:
            return None
        prices.sort()
        trim    = max(1, len(prices) // 10)
        trimmed = prices[trim:-trim] if len(prices) > 4 else prices
        median  = trimmed[len(trimmed) // 2]
        log.info(f"  eBay sold avg: £{median:.2f} ({len(prices)} sales)")
        return round(median, 2)
    except Exception as e:
        log.warning(f"  eBay API error: {e}")
        return None


def search_ebay(card_name: str, grade: str = "raw") -> list[dict]:
    if EBAY_APP_ID:
        return _search_ebay_api(card_name, grade)
    return _search_ebay_scrape(card_name, grade)


def _search_ebay_api(card_name: str, grade: str) -> list[dict]:
    grade_str = "" if grade in ("raw", "sealed") else grade
    query     = f"One Piece {card_name} {grade_str}".strip()
    params = {
        "OPERATION-NAME":                 "findItemsAdvanced",
        "SERVICE-VERSION":                "1.0.0",
        "SECURITY-APPNAME":               EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT":           "JSON",
        "keywords":                       query,
        "categoryId":                     "2536",
        "itemFilter(0).name":             "ListingType",
        "itemFilter(0).value":            "FixedPrice",
        "itemFilter(1).name":             "Currency",
        "itemFilter(1).value":            "GBP",
        "sortOrder":                      "StartTimeNewest",
        "paginationInput.entriesPerPage": "50",
    }
    try:
        resp  = requests.get("https://svcs.ebay.com/services/search/FindingService/v1",
                             params=params, timeout=15)
        items = (resp.json().get("findItemsAdvancedResponse", [{}])[0]
                            .get("searchResult", [{}])[0]
                            .get("item", []))
        results = []
        for item in items:
            try:
                results.append({
                    "title":    item["title"][0],
                    "price":    float(item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"]),
                    "url":      item["viewItemURL"][0],
                    "platform": "eBay",
                })
            except:
                pass
        log.info(f"  eBay API: {len(results)} listings")
        return results
    except Exception as e:
        log.warning(f"  eBay API listing error: {e}")
        return []


def _search_ebay_scrape(card_name: str, grade: str) -> list[dict]:
    query  = f"One Piece {card_name} {grade}".strip()
    params = {"_nkw": query, "_sacat": "2536", "LH_BIN": "1", "_sop": "10", "_ipg": "60"}
    resp   = polite_get("https://www.ebay.co.uk/sch/i.html", params=params)
    if not resp or resp.status_code != 200:
        log.warning(f"  eBay scrape blocked — add EBAY_APP_ID to Railway to fix")
        return []
    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".s-item"):
        title_el = item.select_one(".s-item__title")
        price_el = item.select_one(".s-item__price")
        link_el  = item.select_one(".s-item__link")
        if not (title_el and price_el and link_el):
            continue
        title = title_el.text.strip()
        if title.lower() == "shop on ebay":
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.text.split(" to ")[0].replace(",", ""))
        if not m:
            continue
        try:
            results.append({"title": title, "price": float(m.group()),
                            "url": link_el["href"].split("?")[0], "platform": "eBay"})
        except:
            pass
    log.info(f"  eBay scrape: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# OTHER PLATFORMS
# ─────────────────────────────────────────────────────────────

def search_tcgplayer(card_name: str, grade: str = "raw") -> list[dict]:
    query = card_name.replace(" ", "+")
    url   = f"https://www.tcgplayer.com/search/one-piece-card-game/product?q={query}&view=grid"
    resp  = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select(".search-result, .product-card__product"):
        title_el = card.select_one(".product-card__title, h3")
        price_el = card.select_one(".product-card__market-price, [class*='price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"\d+\.?\d*", price_el.get_text().replace("$", "").replace(",", ""))
        if not m:
            continue
        try:
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://www.tcgplayer.com" + href
            results.append({"title": title_el.get_text(strip=True),
                            "price": round(float(m.group()) * 0.79, 2),
                            "url": href, "platform": "TCGPlayer"})
        except:
            pass
    log.info(f"  TCGPlayer: {len(results)} listings")
    return results


def search_cardmarket(card_name: str, grade: str = "raw") -> list[dict]:
    query = re.sub(r"\b(english|sealed|psa \d+|bgs \d+|cgc \d+|raw)\b",
                   "", card_name.lower()).strip().replace(" ", "+")
    url   = f"https://www.cardmarket.com/en/OnePiece/Products/Search?searchString={query}"
    resp  = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".card, .article-row, [class*='product-card']"):
        title_el = item.select_one("h2 a, .product-name a, [class*='name'] a")
        price_el = item.select_one("[class*='price']")
        link_el  = item.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"\d+\.?\d*", price_el.get_text().replace("€", "").replace(",", "."))
        if not m:
            continue
        try:
            href = link_el.get("href", "") if link_el else ""
            if not href.startswith("http"):
                href = "https://www.cardmarket.com" + href
            results.append({"title": title_el.get_text(strip=True),
                            "price": round(float(m.group()) * 0.85, 2),
                            "url": href, "platform": "Cardmarket"})
        except:
            pass
    log.info(f"  Cardmarket: {len(results)} listings")
    return results[:10]


def search_courtyard(card_name: str, grade: str = "raw") -> list[dict]:
    query = f"one piece {card_name}".replace(" ", "%20")
    url   = f"https://www.courtyard.io/marketplace/search?q={query}"
    resp  = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("[data-testid='listing-card'], .listing-card, .product-card"):
        title_el = card.select_one("h2, h3, [class*='title']")
        price_el = card.select_one("[class*='price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",", ""))
        if not m:
            continue
        try:
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://www.courtyard.io" + href
            results.append({"title": title_el.text.strip(),
                            "price": round(float(m.group()) * 0.79, 2),
                            "url": href, "platform": "Courtyard"})
        except:
            pass
    log.info(f"  Courtyard: {len(results)} listings")
    return results


def search_beezie(card_name: str, grade: str = "raw") -> list[dict]:
    query = f"one piece {card_name}".replace(" ", "%20")
    url   = f"https://beezie.com/marketplace?search={query}"
    resp  = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("[class*='product-card'], [class*='CollectibleCard']"):
        title_el = card.select_one("h2, h3, [class*='title']")
        price_el = card.select_one("[class*='price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.get_text().replace(",", ""))
        if not m:
            continue
        try:
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://beezie.com" + href
            results.append({"title": title_el.get_text(strip=True),
                            "price": round(float(m.group()) * 0.79, 2),
                            "url": href, "platform": "Beezie"})
        except:
            pass
    log.info(f"  Beezie: {len(results)} listings")
    return results


def search_phygitals(card_name: str, grade: str = "raw") -> list[dict]:
    url  = f"https://phygitals.com/marketplace?search={card_name.replace(' ', '+')}"
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select(".product-card, [class*='card']"):
        title_el = card.select_one("h2, h3, [class*='title']")
        price_el = card.select_one("[class*='price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",", ""))
        if not m:
            continue
        try:
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://phygitals.com" + href
            results.append({"title": title_el.text.strip(),
                            "price": round(float(m.group()) * 0.79, 2),
                            "url": href, "platform": "Phygitals"})
        except:
            pass
    log.info(f"  Phygitals: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────────────────────

def run_scan(watchlist: list[dict], threshold: float = 0.82) -> list[dict]:
    all_deals = []

    for card in watchlist:
        name  = card["name"]
        grade = card.get("grade", "raw")
        log.info(f"\n📦 Scanning: {name} [{grade}]")

        market = get_market_price(name, grade)

        if not market or market < 1:
            log.warning(f"  No market price — skipping")
            continue

        for listings in [
            search_ebay(name, grade),
            search_tcgplayer(name, grade),
            search_cardmarket(name, grade),
            search_beezie(name, grade),
            search_courtyard(name, grade),
            search_phygitals(name, grade),
        ]:
            for item in listings:
                if item["price"] <= 0:
                    continue
                ratio = item["price"] / market
                if ratio < threshold:
                    all_deals.append({
                        "card":         name,
                        "grade":        grade,
                        "platform":     item["platform"],
                        "listed":       item["price"],
                        "market":       market,
                        "discount_pct": round((1 - ratio) * 100),
                        "url":          item["url"],
                        "title":        item["title"],
                        "trend":        "unknown",
                        "price_3m_ago": None,
                    })

    return sorted(all_deals, key=lambda x: x["discount_pct"], reverse=True)


