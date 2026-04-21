import os, re, json, time, random, logging
import requests, httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

EBAY_APP_ID = os.getenv("EBAY_APP_ID", "")

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"},
]

def get_headers():
    return random.choice(HEADERS_POOL)

def polite_get(url, params=None, timeout=15):
    time.sleep(random.uniform(1.0, 2.5))
    try:
        return httpx.get(url, headers=get_headers(), params=params,
                         follow_redirects=True, timeout=timeout)
    except Exception as e:
        log.warning(f"Request failed {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# PRICECHARTING
# ─────────────────────────────────────────────────────────────

# Map card name keywords to correct PriceCharting game slugs
PC_GAME_SLUGS = {
    "op01": "one-piece-romance-dawn",
    "op02": "one-piece-paramount-war",
    "op03": "one-piece-pillars-of-strength",
    "op04": "one-piece-kingdoms-of-intrigue",
    "op05": "one-piece-awakening-of-the-new-era",
    "op06": "one-piece-wings-of-the-captain",
    "op07": "one-piece-500-years-in-the-future",
    "op08": "one-piece-two-legends",
    "op09": "one-piece-emperors-in-the-new-world",
    "op10": "one-piece-royal-blood",
    "op11": "one-piece-a-fist-of-divine-speed",
    "op12": "one-piece-legacy-of-the-master",
    "op13": "one-piece-carrying-on-his-will",
    "eb01": "one-piece-memorial-collection",
    "eb02": "one-piece-anime-25th-collection",
    "prb01": "one-piece-one-piece-card-the-best",
    "st01": "one-piece-starter-deck-straw-hat-crew",
    "st10": "one-piece-starter-deck-rising-three-captains",
    "booster box": "one-piece-sealed",
    "sealed": "one-piece-sealed",
    "anniversary": "one-piece-promo",
    "promo": "one-piece-promo",
}


def get_pc_game_slug(card_name: str) -> str:
    name_lower = card_name.lower()
    for key, slug in PC_GAME_SLUGS.items():
        if key in name_lower:
            return slug
    return "one-piece-promo"


def build_pricecharting_url(card_name: str) -> str:
    slug = card_name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    game = get_pc_game_slug(card_name)
    return f"https://www.pricecharting.com/game/{game}/{slug}"


def get_pricecharting_data(card_name: str) -> dict:
    url  = build_pricecharting_url(card_name)
    resp = polite_get(url)

    # If direct URL fails, try PriceCharting search
    if not resp or resp.status_code != 200:
        resp = _pricecharting_search(card_name)

    if not resp or resp.status_code != 200:
        log.warning(f"PriceCharting: no page for '{card_name}'")
        return {}

    soup   = BeautifulSoup(resp.text, "html.parser")
    result = {"url": url, "prices": {}, "history": [], "trend": "unknown", "price_3m_ago": None}

    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) >= 2:
            grade_raw = cells[0].get_text(strip=True)
            price_raw = cells[-1].get_text(strip=True)
            price_raw = price_raw.replace("£","").replace("$","").replace(",","").strip()
            try:
                result["prices"][grade_raw] = float(price_raw)
            except ValueError:
                pass

    for script in soup.find_all("script"):
        text  = script.string or ""
        match = re.search(r"var\s+chartData\s*=\s*(\[[\s\S]*?\]);", text)
        if not match:
            match = re.search(r"chartData[\"']?\s*:\s*(\[[\s\S]*?\])", text)
        if match:
            try:
                raw    = json.loads(match.group(1))
                points = []
                for p in raw:
                    if isinstance(p, list) and len(p) >= 2 and p[1] is not None:
                        points.append({"timestamp": p[0], "price_gbp": round((p[1]/100)*0.79, 2)})
                result["history"] = points
                if len(points) >= 10:
                    recent = [p["price_gbp"] for p in points[-8:]]
                    older  = [p["price_gbp"] for p in points[-20:-8]]
                    if older:
                        avg_r = sum(recent)/len(recent)
                        avg_o = sum(older)/len(older)
                        pct   = ((avg_r - avg_o)/avg_o)*100
                        result["trend"] = "rising" if pct > 5 else ("falling" if pct < -5 else "stable")
                    if len(points) >= 13:
                        result["price_3m_ago"] = points[-13]["price_gbp"]
            except Exception as e:
                log.warning(f"Chart parse error: {e}")

    return result


def _pricecharting_search(card_name: str):
    """Search PriceCharting and return the response of the first result page."""
    # Clean search query
    query = re.sub(r"(english|sealed|raw|psa \d+|bgs \d+|cgc \d+)", "", card_name.lower()).strip()
    resp  = polite_get(
        "https://www.pricecharting.com/search-products",
        params={"q": f"one piece {query}", "type": "prices"}
    )
    if not resp or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    # Get first matching result link
    link = soup.select_one("table.games td.title a, #games td.title a")
    if link and link.get("href"):
        return polite_get(f"https://www.pricecharting.com{link['href']}")
    return None


def get_current_price_from_pc(pc_data: dict, grade: str = "raw") -> float | None:
    prices = pc_data.get("prices", {})
    if not prices:
        return None
    grade_map = {
        "raw":             ["Ungraded","ungraded","Complete","NM","Loose"],
        "sealed":          ["New","Sealed","New (Sealed)","Graded"],
        "psa 10":          ["PSA 10","Grade 10","Graded 10"],
        "psa 9":           ["PSA 9","Grade 9","Graded 9"],
        "psa 8":           ["PSA 8"],
        "bgs 9.5":         ["BGS 9.5"],
        "bgs black label": ["BGS Black Label","Black Label"],
        "cgc 10":          ["CGC 10"],
    }
    for label in grade_map.get(grade.lower(), [grade]):
        if label in prices:
            return prices[label]
    # Return first valid price as fallback
    for v in prices.values():
        if isinstance(v, (int, float)) and v > 0:
            return v
    return None


# ─────────────────────────────────────────────────────────────
# EBAY FINDING API (official — not blocked by Railway IPs)
# ─────────────────────────────────────────────────────────────

def get_ebay_sold_avg_api(card_name: str, grade: str = "raw") -> float | None:
    """
    Uses the official eBay Finding API to get sold prices.
    Requires EBAY_APP_ID — free from developer.ebay.com
    Not blocked by Railway because it uses API keys not browser requests.
    """
    if not EBAY_APP_ID:
        log.warning("EBAY_APP_ID not set — cannot use eBay API for price lookup")
        return None

    # Clean query
    grade_str = "" if grade == "raw" else grade
    query     = f"One Piece {card_name} {grade_str}".strip()

    params = {
        "OPERATION-NAME":           "findCompletedItems",
        "SERVICE-VERSION":          "1.0.0",
        "SECURITY-APPNAME":         EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT":     "JSON",
        "keywords":                 query,
        "categoryId":               "2536",
        "itemFilter(0).name":       "SoldItemsOnly",
        "itemFilter(0).value":      "true",
        "itemFilter(1).name":       "ListingType",
        "itemFilter(1).value":      "FixedPrice",
        "itemFilter(2).name":       "Currency",
        "itemFilter(2).value":      "GBP",
        "sortOrder":                "EndTimeSoonest",
        "paginationInput.entriesPerPage": "20",
    }

    try:
        resp = requests.get(
            "https://svcs.ebay.com/services/search/FindingService/v1",
            params=params,
            timeout=15
        )
        if resp.status_code != 200:
            log.warning(f"eBay API error: {resp.status_code}")
            return None

        data  = resp.json()
        items = (data.get("findCompletedItemsResponse", [{}])[0]
                     .get("searchResult", [{}])[0]
                     .get("item", []))

        prices = []
        for item in items:
            try:
                price = float(item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"])
                if price > 0:
                    prices.append(price)
            except (KeyError, IndexError, ValueError):
                pass

        if not prices:
            log.info(f"  eBay API: no sold results for '{query}'")
            return None

        prices.sort()
        trim    = max(1, len(prices)//10)
        trimmed = prices[trim:-trim] if len(prices) > 4 else prices
        median  = trimmed[len(trimmed)//2]
        log.info(f"  eBay API sold avg [{grade}]: £{median:.2f} ({len(prices)} sales)")
        return round(median, 2)

    except Exception as e:
        log.warning(f"eBay API error: {e}")
        return None


def get_market_price(card_name: str, grade: str, pc_data: dict) -> float | None:
    # 1. Try PriceCharting first
    price = get_current_price_from_pc(pc_data, grade)
    if price and price > 1:
        log.info(f"  PriceCharting [{grade}]: £{price:.2f}")
        return round(price, 2)

    # 2. Fallback to eBay Finding API (not blocked)
    log.info(f"  PriceCharting had no price — trying eBay API")
    price = get_ebay_sold_avg_api(card_name, grade)
    return price


# ─────────────────────────────────────────────────────────────
# EBAY ACTIVE LISTINGS — Finding API
# ─────────────────────────────────────────────────────────────

def search_ebay(card_name: str, grade: str = "raw") -> list[dict]:
    """
    Uses eBay Finding API to get active BIN listings.
    Falls back to scraping if no API key set.
    """
    if EBAY_APP_ID:
        return _search_ebay_api(card_name, grade)
    return _search_ebay_scrape(card_name, grade)


def _search_ebay_api(card_name: str, grade: str) -> list[dict]:
    grade_str = "" if grade in ("raw", "sealed") else grade
    query     = f"One Piece {card_name} {grade_str}".strip()

    params = {
        "OPERATION-NAME":           "findItemsAdvanced",
        "SERVICE-VERSION":          "1.0.0",
        "SECURITY-APPNAME":         EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT":     "JSON",
        "keywords":                 query,
        "categoryId":               "2536",
        "itemFilter(0).name":       "ListingType",
        "itemFilter(0).value":      "FixedPrice",
        "itemFilter(1).name":       "Currency",
        "itemFilter(1).value":      "GBP",
        "sortOrder":                "StartTimeNewest",
        "paginationInput.entriesPerPage": "50",
    }

    try:
        resp = requests.get(
            "https://svcs.ebay.com/services/search/FindingService/v1",
            params=params, timeout=15
        )
        if resp.status_code != 200:
            return []

        data  = resp.json()
        items = (data.get("findItemsAdvancedResponse", [{}])[0]
                     .get("searchResult", [{}])[0]
                     .get("item", []))

        results = []
        for item in items:
            try:
                title = item["title"][0]
                price = float(item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"])
                url   = item["viewItemURL"][0]
                if price > 0:
                    results.append({"title": title, "price": price, "url": url, "platform": "eBay"})
            except (KeyError, IndexError, ValueError):
                pass

        log.info(f"  eBay API: {len(results)} listings")
        return results

    except Exception as e:
        log.warning(f"eBay API listing error: {e}")
        return []


def _search_ebay_scrape(card_name: str, grade: str) -> list[dict]:
    """Fallback scraper — may be blocked on Railway."""
    query  = f"One Piece {card_name} {grade}".strip()
    params = {"_nkw": query, "_sacat": "2536", "LH_BIN": "1", "_sop": "10", "_ipg": "60"}
    resp   = polite_get("https://www.ebay.co.uk/sch/i.html", params=params)
    if not resp or resp.status_code != 200:
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
        m = re.search(r"[\d,]+\.?\d*", price_el.text.split(" to ")[0].replace(",",""))
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
# TCGPLAYER
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
        title_el = card.select_one(".product-card__title, h3, [class*='title']")
        price_el = card.select_one(".product-card__market-price, [class*='price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"\d+\.?\d*", price_el.get_text().replace("$","").replace(",",""))
        if not m:
            continue
        try:
            gbp  = round(float(m.group()) * 0.79, 2)
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://www.tcgplayer.com" + href
            results.append({"title": title_el.get_text(strip=True), "price": gbp,
                            "url": href, "platform": "TCGPlayer"})
        except:
            pass

    log.info(f"  TCGPlayer: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# BEEZIE
# ─────────────────────────────────────────────────────────────

def search_beezie(card_name: str, grade: str = "raw") -> list[dict]:
    query = f"one piece {card_name}".replace(" ", "%20")
    url   = f"https://beezie.com/marketplace?search={query}&category=trading-cards"
    resp  = polite_get(url)
    if not resp or resp.status_code != 200:
        return []

    soup    = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("[class*='product-card'], [class*='listing-card'], [class*='CollectibleCard']"):
        title_el = card.select_one("h2, h3, [class*='title'], [class*='name']")
        price_el = card.select_one("[class*='price'], [class*='Price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.get_text().replace(",",""))
        if not m:
            continue
        try:
            gbp  = round(float(m.group()) * 0.79, 2)
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://beezie.com" + href
            results.append({"title": title_el.get_text(strip=True), "price": gbp,
                            "url": href, "platform": "Beezie"})
        except:
            pass

    log.info(f"  Beezie: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# COURTYARD
# ─────────────────────────────────────────────────────────────

def search_courtyard(card_name: str, grade: str = "raw") -> list[dict]:
    query = f"one piece {card_name} {grade}".replace(" ", "%20")
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
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",",""))
        if not m:
            continue
        try:
            gbp  = round(float(m.group()) * 0.79, 2)
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://www.courtyard.io" + href
            results.append({"title": title_el.text.strip(), "price": gbp,
                            "url": href, "platform": "Courtyard"})
        except:
            pass

    log.info(f"  Courtyard: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# PHYGITALS
# ─────────────────────────────────────────────────────────────

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
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",",""))
        if not m:
            continue
        try:
            gbp  = round(float(m.group()) * 0.79, 2)
            href = link_el["href"] if link_el else url
            if not href.startswith("http"):
                href = "https://phygitals.com" + href
            results.append({"title": title_el.text.strip(), "price": gbp,
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

        pc_data = get_pricecharting_data(name)
        market  = get_market_price(name, grade, pc_data)

        if not market or market < 1:
            log.warning(f"  No market price — skipping")
            continue

        for listings in [
            search_ebay(name, grade),
            search_tcgplayer(name, grade),
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
                        "trend":        pc_data.get("trend", "unknown"),
                        "price_3m_ago": pc_data.get("price_3m_ago"),
                    })

    return sorted(all_deals, key=lambda x: x["discount_pct"], reverse=True)

