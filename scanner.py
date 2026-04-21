import os, re, json, time, random, logging
import requests, httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

HEADERS_POOL = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"},
]

def get_headers():
    return random.choice(HEADERS_POOL)

def polite_get(url, params=None, timeout=15):
    time.sleep(random.uniform(1.2, 3.0))
    try:
        return httpx.get(url, headers=get_headers(), params=params,
                         follow_redirects=True, timeout=timeout)
    except Exception as e:
        log.warning(f"Request failed {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# PRICECHARTING — current + historical
# ─────────────────────────────────────────────────────────────

def build_pricecharting_url(card_name: str) -> str:
    slug = card_name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    return f"https://www.pricecharting.com/game/one-piece-promo/{slug}"


def get_pricecharting_data(card_name: str) -> dict:
    url = build_pricecharting_url(card_name)
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        log.warning(f"PriceCharting: no page for '{card_name}'")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
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
        text = script.string or ""
        match = re.search(r"var\s+chartData\s*=\s*(\[[\s\S]*?\]);", text)
        if not match:
            match = re.search(r"chartData[\"']?\s*:\s*(\[[\s\S]*?\])", text)
        if match:
            try:
                raw = json.loads(match.group(1))
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


def get_current_price_from_pc(pc_data: dict, grade: str = "raw") -> float | None:
    prices = pc_data.get("prices", {})
    if not prices:
        return None
    grade_map = {
        "raw":    ["Ungraded","ungraded","Complete","NM"],
        "psa 10": ["PSA 10","Grade 10","Graded 10"],
        "psa 9":  ["PSA 9","Grade 9","Graded 9"],
        "psa 8":  ["PSA 8","Grade 8","Graded 8"],
        "bgs 9.5":["BGS 9.5","Beckett 9.5"],
        "cgc 10": ["CGC 10","CGC Pristine"],
    }
    for label in grade_map.get(grade.lower(), [grade]):
        if label in prices:
            return prices[label]
    for v in prices.values():
        if isinstance(v, (int, float)) and v > 0:
            return v
    return None


# ─────────────────────────────────────────────────────────────
# EBAY SOLD AVERAGE (fallback)
# ─────────────────────────────────────────────────────────────

def get_ebay_sold_avg(card_name: str, grade: str = "raw") -> float | None:
    query = f"One Piece {card_name} {grade}".strip()
    params = {"_nkw": query, "_sacat": "2536", "LH_Complete": "1", "LH_Sold": "1", "_sop": "13"}
    resp = polite_get("https://www.ebay.co.uk/sch/i.html", params=params)
    if not resp or resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    prices = []
    for item in soup.select(".s-item"):
        el = item.select_one(".s-item__price")
        if not el:
            continue
        m = re.search(r"[\d,]+\.?\d*", el.text.split(" to ")[0].replace(",",""))
        if m:
            try: prices.append(float(m.group()))
            except: pass
    if not prices:
        return None
    prices.sort()
    trim = max(1, len(prices)//10)
    trimmed = prices[trim:-trim] if len(prices) > 4 else prices
    return round(trimmed[len(trimmed)//2], 2)


def get_market_price(card_name: str, grade: str, pc_data: dict) -> float | None:
    price = get_current_price_from_pc(pc_data, grade)
    if price and price > 1:
        log.info(f"  PriceCharting [{grade}]: £{price:.2f}")
        return round(price, 2)
    log.info(f"  Falling back to eBay sold avg")
    price = get_ebay_sold_avg(card_name, grade)
    if price:
        log.info(f"  eBay sold avg [{grade}]: £{price:.2f}")
    return price


# ─────────────────────────────────────────────────────────────
# EBAY ACTIVE LISTINGS
# ─────────────────────────────────────────────────────────────

def search_ebay(card_name: str, grade: str = "raw") -> list[dict]:
    query = f"One Piece {card_name} {grade}".strip()
    params = {"_nkw": query, "_sacat": "2536", "LH_BIN": "1", "_sop": "10", "_ipg": "60"}
    resp = polite_get("https://www.ebay.co.uk/sch/i.html", params=params)
    if not resp or resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
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
            price = float(m.group())
        except:
            continue
        results.append({"title": title, "price": price,
                        "url": link_el["href"].split("?")[0], "platform": "eBay"})
    log.info(f"  eBay: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# TCGPLAYER
# ─────────────────────────────────────────────────────────────

def search_tcgplayer(card_name: str, grade: str = "raw") -> list[dict]:
    """
    Searches TCGPlayer for One Piece card listings.
    TCGPlayer prices are in USD — converted to GBP.
    """
    query = f"{card_name}".replace(" ", "+")
    url = f"https://www.tcgplayer.com/search/one-piece-card-game/product?q={query}&productLineName=one-piece-card-game&view=grid"
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        log.info(f"  TCGPlayer: no response")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(".search-result, [class*='search-result'], .product-card__product"):
        title_el = card.select_one(".product-card__title, h3, [class*='title']")
        price_el = card.select_one(".product-card__market-price, [class*='price'], .inventory__price-with-shipping")
        link_el  = card.select_one("a")

        if not (title_el and price_el):
            continue

        price_text = price_el.get_text(strip=True).replace("$","").replace(",","").strip()
        # Handle "Market Price: $X.XX" format
        price_text = re.sub(r"[^\d.]", "", price_text.split("Market Price")[-1])
        m = re.search(r"\d+\.?\d*", price_text)
        if not m:
            continue
        try:
            usd = float(m.group())
            gbp = round(usd * 0.79, 2)
        except:
            continue

        href = link_el["href"] if link_el else url
        if not href.startswith("http"):
            href = "https://www.tcgplayer.com" + href

        results.append({
            "title": title_el.get_text(strip=True),
            "price": gbp,
            "url": href,
            "platform": "TCGPlayer"
        })

    log.info(f"  TCGPlayer: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# BEEZIE
# ─────────────────────────────────────────────────────────────

def search_beezie(card_name: str, grade: str = "raw") -> list[dict]:
    """
    Searches Beezie marketplace for One Piece cards.
    All items are vaulted in Brink's and pre-authenticated.
    Prices in USD — converted to GBP.
    """
    query = f"one piece {card_name}".replace(" ", "%20")
    url = f"https://beezie.com/marketplace?search={query}&category=trading-cards"
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        log.info(f"  Beezie: no response")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # ⚠️ Beezie uses React — if results are empty, inspect beezie.com/marketplace
    # in DevTools and update these selectors to match current class names
    for card in soup.select(
        "[class*='product-card'], [class*='listing-card'], "
        "[class*='item-card'], [data-testid*='card'], [class*='CollectibleCard']"
    ):
        title_el = card.select_one("h2, h3, [class*='title'], [class*='name']")
        price_el = card.select_one("[class*='price'], [class*='Price']")
        link_el  = card.select_one("a")

        if not (title_el and price_el):
            continue

        m = re.search(r"[\d,]+\.?\d*", price_el.get_text().replace(",",""))
        if not m:
            continue
        try:
            usd = float(m.group())
            gbp = round(usd * 0.79, 2)
        except:
            continue

        href = link_el["href"] if link_el else url
        if not href.startswith("http"):
            href = "https://beezie.com" + href

        results.append({
            "title": title_el.get_text(strip=True),
            "price": gbp,
            "url": href,
            "platform": "Beezie"
        })

    log.info(f"  Beezie: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# COURTYARD
# ─────────────────────────────────────────────────────────────

def search_courtyard(card_name: str, grade: str = "raw") -> list[dict]:
    query = f"one piece {card_name} {grade}".replace(" ", "%20")
    url = f"https://www.courtyard.io/marketplace/search?q={query}"
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("[data-testid='listing-card'], .listing-card, .product-card"):
        title_el = card.select_one("h2, h3, .title, [class*='title']")
        price_el = card.select_one("[class*='price'], .price")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",",""))
        if not m:
            continue
        try:
            gbp = round(float(m.group()) * 0.79, 2)
        except:
            continue
        href = link_el["href"] if link_el else url
        if not href.startswith("http"):
            href = "https://www.courtyard.io" + href
        results.append({"title": title_el.text.strip(), "price": gbp, "url": href, "platform": "Courtyard"})
    log.info(f"  Courtyard: {len(results)} listings")
    return results


# ─────────────────────────────────────────────────────────────
# PHYGITALS
# ─────────────────────────────────────────────────────────────

def search_phygitals(card_name: str, grade: str = "raw") -> list[dict]:
    query = card_name.replace(" ", "+")
    url = f"https://phygitals.com/marketplace?search={query}"
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select(".product-card, .listing-item, [class*='card']"):
        title_el = card.select_one("h2, h3, [class*='name'], [class*='title']")
        price_el = card.select_one("[class*='price']")
        link_el  = card.select_one("a")
        if not (title_el and price_el):
            continue
        m = re.search(r"[\d,]+\.?\d*", price_el.text.replace(",",""))
        if not m:
            continue
        try:
            gbp = round(float(m.group()) * 0.79, 2)
        except:
            continue
        href = link_el["href"] if link_el else url
        if not href.startswith("http"):
            href = "https://phygitals.com" + href
        results.append({"title": title_el.text.strip(), "price": gbp, "url": href, "platform": "Phygitals"})
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

        all_platforms = [
            search_ebay(name, grade),
            search_tcgplayer(name, grade),
            search_beezie(name, grade),
            search_courtyard(name, grade),
            search_phygitals(name, grade),
        ]

        for listings in all_platforms:
            for item in listings:
                if item["price"] <= 0:
                    continue
                ratio = item["price"] / market
                if ratio < threshold:
                    all_deals.append({
                        "card":        name,
                        "grade":       grade,
                        "platform":    item["platform"],
                        "listed":      item["price"],
                        "market":      market,
                        "discount_pct": round((1 - ratio) * 100),
                        "url":         item["url"],
                        "title":       item["title"],
                        "trend":       pc_data.get("trend", "unknown"),
                        "price_3m_ago": pc_data.get("price_3m_ago"),
                    })

    return sorted(all_deals, key=lambda x: x["discount_pct"], reverse=True)
