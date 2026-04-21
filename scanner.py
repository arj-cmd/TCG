import os, re, json, time, random, logging
import requests, httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

EBAY_APP_ID         = os.getenv("EBAY_APP_ID", "")
PRICECHARTING_TOKEN = os.getenv("PRICECHARTING_TOKEN", "")
TCGAPI_DEV_KEY      = os.getenv("TCGAPI_DEV_KEY", "")

# Price cache — avoids re-fetching same card within 2 hours
_price_cache: dict = {}
CACHE_TTL_SECONDS = 7200

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"},
]

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
# PRICECHARTING OFFICIAL API
# Works from Railway — uses token not browser session
# ─────────────────────────────────────────────────────────────

def get_pricecharting_api(card_name: str) -> dict:
    """
    Uses PriceCharting's official /api/product endpoint.
    Requires PRICECHARTING_TOKEN (from pricecharting.com subscription).
    Returns prices in USD — we convert to GBP.
    Not blocked by Railway because it uses API token authentication.
    """
    if not PRICECHARTING_TOKEN:
        return {}

    # Clean the search query
    query = re.sub(r"(english|sealed|psa \d+\.?\d*|bgs \d+\.?\d*|cgc \d+\.?\d*)", "",
                   card_name.lower()).strip()
    query = re.sub(r"\s+", " ", query)

    try:
        resp = requests.get(
            "https://www.pricecharting.com/api/product",
            params={"t": PRICECHARTING_TOKEN, "q": f"one piece {query}"},
            timeout=10
        )
        if resp.status_code != 200:
            log.warning(f"PriceCharting API error: {resp.status_code}")
            return {}

        data = resp.json()
        if data.get("status") != "success":
            return {}

        # PriceCharting returns prices in cents
        def cents_to_gbp(cents):
            if not cents or cents <= 0:
                return None
            return round((cents / 100) * 0.79, 2)

        prices = {
            "Ungraded":   cents_to_gbp(data.get("loose-price")),
            "New":        cents_to_gbp(data.get("new-price")),
            "PSA 10":     cents_to_gbp(data.get("grade-10-price")),
            "PSA 9":      cents_to_gbp(data.get("grade-9-price")),
            "PSA 8":      cents_to_gbp(data.get("grade-8-price")),
            "PSA 7":      cents_to_gbp(data.get("grade-7-price")),
            "BGS 9.5":    cents_to_gbp(data.get("grade-9-5-price")),
        }
        # Remove None values
        prices = {k: v for k, v in prices.items() if v}

        if prices:
            log.info(f"  PriceCharting API: found {len(prices)} price points for '{query}'")

        return {
            "prices":       prices,
            "history":      [],
            "trend":        "unknown",
            "price_3m_ago": None,
            "product_name": data.get("product-name", ""),
            "product_id":   data.get("id", ""),
        }

    except Exception as e:
        log.warning(f"PriceCharting API error: {e}")
        return {}


def get_pricecharting_scrape(card_name: str) -> dict:
    """
    Fallback: scrape PriceCharting page directly.
    May work depending on Railway IP rotation.
    """
    game_slugs = {
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
        "st01": "one-piece-starter-deck-straw-hat-crew",
        "st10": "one-piece-starter-deck-rising-three-captains",
    }

    name_lower = card_name.lower()
    game       = "one-piece-promo"
    for key, slug in game_slugs.items():
        if key in name_lower:
            game = slug
            break

    slug = re.sub(r"[^a-z0-9\s-]", "", name_lower)
    slug = re.sub(r"\s+", "-", slug.strip())
    url  = f"https://www.pricecharting.com/game/{game}/{slug}"

    resp = polite_get(url)

    # Try search if direct URL fails
    if not resp or resp.status_code != 200:
        query = re.sub(r"(english|sealed)", "", name_lower).strip()
        resp  = polite_get(
            "https://www.pricecharting.com/search-products",
            params={"q": f"one piece {query}", "type": "prices"}
        )
        if resp and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            link = soup.select_one("table.games td.title a, #games td.title a")
            if link and link.get("href"):
                resp = polite_get(f"https://www.pricecharting.com{link['href']}")

    if not resp or resp.status_code != 200:
        return {}

    soup   = BeautifulSoup(resp.text, "html.parser")
    result = {"prices": {}, "history": [], "trend": "unknown", "price_3m_ago": None}

    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) >= 2:
            grade_raw = cells[0].get_text(strip=True)
            price_raw = cells[-1].get_text(strip=True)
            price_raw = price_raw.replace("£","").replace("$","").replace(",","").strip()
            try:
                val = float(price_raw)
                if val > 0:
                    result["prices"][grade_raw] = round(val * 0.79, 2)
            except ValueError:
                pass

    for script in soup.find_all("script"):
        text  = script.string or ""
        match = re.search(r"var\s+chartData\s*=\s*(\[[\s\S]*?\]);", text)
        if match:
            try:
                raw    = json.loads(match.group(1))
                points = [{"timestamp": p[0], "price_gbp": round((p[1]/100)*0.79, 2)}
                          for p in raw if isinstance(p, list) and len(p) >= 2 and p[1]]
                result["history"] = points
                if len(points) >= 10:
                    recent = [p["price_gbp"] for p in points[-8:]]
                    older  = [p["price_gbp"] for p in points[-20:-8]]
                    if older:
                        chg = ((sum(recent)/len(recent)) - (sum(older)/len(older))) / (sum(older)/len(older)) * 100
                        result["trend"] = "rising" if chg > 5 else ("falling" if chg < -5 else "stable")
                if len(points) >= 13:
                    result["price_3m_ago"] = points[-13]["price_gbp"]
            except Exception:
                pass

    return result


def get_pricecharting_data(card_name: str) -> dict:
    """Try official API first, fall back to scraping."""
    if PRICECHARTING_TOKEN:
        data = get_pricecharting_api(card_name)
        if data.get("prices"):
            return data
    return get_pricecharting_scrape(card_name)


def get_current_price_from_pc(pc_data: dict, grade: str = "raw") -> float | None:
    prices = pc_data.get("prices", {})
    if not prices:
        return None

    grade_map = {
        "raw":             ["Ungraded","ungraded","Complete","NM","Loose"],
        "sealed":          ["New","Sealed","New (Sealed)"],
        "psa 10":          ["PSA 10","Grade 10","Graded 10"],
        "psa 9":           ["PSA 9","Grade 9","Graded 9"],
        "psa 8":           ["PSA 8","Grade 8"],
        "bgs 9.5":         ["BGS 9.5","Beckett 9.5"],
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
# EBAY FINDING API (ready when approved tomorrow)
# ─────────────────────────────────────────────────────────────

def get_ebay_sold_avg_api(card_name: str, grade: str = "raw") -> float | None:
    if not EBAY_APP_ID:
        return None

    grade_str = "" if grade in ("raw","sealed") else grade
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
        items  = (resp.json().get("findCompletedItemsResponse",[{}])[0]
                             .get("searchResult",[{}])[0]
                             .get("item",[]))
        prices = []
        for item in items:
            try:
                prices.append(float(item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"]))
            except:
                pass
        if not prices:
            return None
        prices.sort()
        trim    = max(1, len(prices)//10)
        trimmed = prices[trim:-trim] if len(prices) > 4 else prices
        median  = trimmed[len(trimmed)//2]
        log.info(f"  eBay API sold avg: £{median:.2f} ({len(prices)} sales)")
        return round(median, 2)
    except Exception as e:
        log.warning(f"eBay API error: {e}")
        return None



# ─────────────────────────────────────────────────────────────
# TCGAPI.DEV — FREE 100 req/day, covers One Piece
# Sign up at tcgapi.dev — instant, no credit card needed
# ─────────────────────────────────────────────────────────────

def get_tcgapi_dev_price(card_name: str, grade: str = "raw") -> float | None:
    if not TCGAPI_DEV_KEY:
        return None
    cache_key = f"tcgdev_{card_name}_{grade}"
    if cache_key in _price_cache:
        cached_at, cached_price = _price_cache[cache_key]
        if time.time() - cached_at < CACHE_TTL_SECONDS:
            log.info(f"  tcgapi.dev [cached]: £{cached_price:.2f}")
            return cached_price
    query = re.sub(r"(english|sealed|psa \d+|bgs \d+|cgc \d+)", "",
                   card_name.lower()).strip()
    try:
        resp = requests.get(
            "https://tcgapi.dev/api/v1/cards/search",
            headers={"Authorization": f"Bearer {TCGAPI_DEV_KEY}"},
            params={"q": f"one piece {query}", "game": "one-piece"},
            timeout=10
        )
        if resp.status_code != 200:
            return None
        cards = resp.json().get("data", [])
        if not cards:
            return None
        card  = cards[0]
        price = card.get("price") or card.get("low_price")
        if price:
            gbp = round(float(price) * 0.79, 2)
            _price_cache[cache_key] = (time.time(), gbp)
            log.info(f"  tcgapi.dev: £{gbp:.2f}")
            return gbp
    except Exception as e:
        log.warning(f"  tcgapi.dev error: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# CARDMARKET LISTINGS — EU marketplace
# ─────────────────────────────────────────────────────────────

def search_cardmarket(card_name: str, grade: str = "raw") -> list[dict]:
    query = re.sub(r"(english|sealed|psa \d+|bgs \d+|cgc \d+|raw)", "",
                   card_name.lower()).strip()
    query = re.sub(r"\s+", "+", query)
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
        m = re.search(r"\d+\.?\d*", price_el.get_text().replace("€","").replace(",","."))
        if not m:
            continue
        try:
            gbp  = round(float(m.group()) * 0.85, 2)
            href = link_el.get("href","") if link_el else ""
            if not href.startswith("http"):
                href = "https://www.cardmarket.com" + href
            results.append({"title": title_el.get_text(strip=True),
                            "price": gbp, "url": href, "platform": "Cardmarket"})
        except:
            pass
    log.info(f"  Cardmarket: {len(results)} listings")
    return results[:10]

def get_market_price(card_name: str, grade: str, pc_data: dict) -> float | None:
    # 1. PriceCharting (API or scrape)
    price = get_current_price_from_pc(pc_data, grade)
    if price and price > 1:
        log.info(f"  Market price [{grade}]: £{price:.2f}")
        return round(price, 2)

    # 2. tcgapi.dev free tier (instant signup, 100 req/day free)
    if TCGAPI_DEV_KEY:
        price = get_tcgapi_dev_price(card_name, grade)
        if price and price > 1:
            return price

    # 3. eBay Finding API (when approved tomorrow)
    if EBAY_APP_ID:
        price = get_ebay_sold_avg_api(card_name, grade)
        if price:
            return price

    log.warning(f"  No market price found for '{card_name}' [{grade}]")
    return None


# ─────────────────────────────────────────────────────────────
# EBAY ACTIVE LISTINGS
# ─────────────────────────────────────────────────────────────

def search_ebay(card_name: str, grade: str = "raw") -> list[dict]:
    """Uses eBay API if available, otherwise scrape."""
    if EBAY_APP_ID:
        return _search_ebay_api(card_name, grade)
    return _search_ebay_scrape(card_name, grade)


def _search_ebay_api(card_name: str, grade: str) -> list[dict]:
    grade_str = "" if grade in ("raw","sealed") else grade
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
        items = (resp.json().get("findItemsAdvancedResponse",[{}])[0]
                            .get("searchResult",[{}])[0]
                            .get("item",[]))
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
        log.warning(f"eBay API listing error: {e}")
        return []


def _search_ebay_scrape(card_name: str, grade: str) -> list[dict]:
    query  = f"One Piece {card_name} {grade}".strip()
    params = {"_nkw": query, "_sacat": "2536", "LH_BIN": "1", "_sop": "10", "_ipg": "60"}
    resp   = polite_get("https://www.ebay.co.uk/sch/i.html", params=params)
    if not resp or resp.status_code != 200:
        log.warning(f"  eBay scrape blocked (503) — add EBAY_APP_ID to fix this")
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
        m = re.search(r"\d+\.?\d*", price_el.get_text().replace("$","").replace(",",""))
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
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",",""))
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
        m = re.search(r"[\d,]+\.?\d*", price_el.get_text().replace(",",""))
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
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",",""))
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

        pc_data = get_pricecharting_data(name)
        market  = get_market_price(name, grade, pc_data)

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
                        "trend":        pc_data.get("trend", "unknown"),
                        "price_3m_ago": pc_data.get("price_3m_ago"),
                    })

    return sorted(all_deals, key=lambda x: x["discount_pct"], reverse=True)


