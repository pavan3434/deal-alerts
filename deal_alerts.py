"""Deal alerts: emails you (one email per deal, with image + link) when shoes /
clothes / Black Friday deals show up or drop in price.

Sources
  1. Slickdeals keyword RSS feeds (covers Nike, Adidas, Amazon, Macy's, ...).
  2. Optional Shopify stores (products.json) - alerts when a product's price drops.

State lives in state.json so each deal is sent only once.
First run ever = "seed" run: records everything currently live, sends nothing
(so you don't get flooded with 200 old deals).

Env vars (set as GitHub Actions secrets):
  GMAIL_USER          your gmail address (sender)
  GMAIL_APP_PASSWORD  16-char Gmail app password
  ALERT_TO            where alerts go (defaults to GMAIL_USER)
  DRY_RUN=1           print instead of sending
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SD_RSS = ("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals"
          "&searchin=first&rss=1&q={q}")
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
STATE_KEEP_DAYS = 30


def fetch(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- Slickdeals
def parse_sd_item(item):
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    body = item.findtext("content:encoded", default="", namespaces=NS) or ""
    m = re.search(r"/f/(\d+)", link)
    deal_id = "sd:" + (m.group(1) if m else link)
    img = re.search(r'<img[^>]+src="([^"]+)"', body)
    thumbs = re.search(r"Thumb Score:\s*([+-]?\d+)", body)
    desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
    desc = html.unescape(re.sub(r"\s+", " ", desc)).strip()
    prices = re.findall(r"\$\s?([\d,]+(?:\.\d{2})?)", title)
    return {
        "id": deal_id,
        "title": html.unescape(title),
        "link": link.split("?")[0],
        "image": img.group(1) if img else "",
        "thumbs": int(thumbs.group(1)) if thumbs else 0,
        "price": "$" + prices[0] if prices else "",
        "details": desc[:300],
        "source": "Slickdeals",
    }


def price_num(s):
    try:
        return float(s.replace("$", "").replace(",", "")) if s else None
    except ValueError:
        return None


def matches(title, words):
    t = title.lower()
    return any(w in t for w in words)


def slickdeals_deals(cfg):
    """Return {category_name: [deal, ...]}, each deal in only one category."""
    excludes = [w.lower() for w in cfg.get("exclude_words", [])]
    min_thumbs = cfg.get("min_thumb_score", 0)
    pool = {}  # id -> deal (all fetched)
    for cat in cfg["categories"]:
        for q in cat["searches"]:
            try:
                xml = fetch(SD_RSS.format(q=urllib.parse.quote_plus(q)),
                            "application/rss+xml")
                root = ET.fromstring(xml)
            except Exception as e:  # one bad feed must not stop the rest
                print(f"  ! feed '{q}' failed: {e}")
                continue
            for it in root.iter("item"):
                d = parse_sd_item(it)
                pool.setdefault(d["id"], d)
            time.sleep(1)

    out = {c["name"]: [] for c in cfg["categories"]}
    for d in pool.values():
        if matches(d["title"], excludes) or d["thumbs"] < min_thumbs:
            continue
        for cat in cfg["categories"]:  # first matching category wins
            must = [w.lower() for w in cat.get("title_must_contain_any", [])]
            also = [w.lower() for w in cat.get("also_must_contain_any", [])]
            if must and not matches(d["title"], must):
                continue
            if also and not matches(d["title"], also):
                continue
            out[cat["name"]].append(d)
            break
    return out


# ---------------------------------------------------------------- Nike.com
def _nike_products(obj):
    if isinstance(obj, dict):
        pr = obj.get("prices")
        if isinstance(pr, dict) and "currentPrice" in pr and obj.get("productCode"):
            yield obj
        for v in obj.values():
            yield from _nike_products(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _nike_products(v)


def nike_deals(cfg):
    """Products from nike.com's own public sale pages (direct nike.com links)."""
    ncfg = cfg.get("nike", {})
    min_off = ncfg.get("min_discount_percent", 0)
    type_to_cat = ncfg.get("product_types", {"FOOTWEAR": "Shoes", "APPAREL": "Clothes"})
    out, done = {}, set()
    if True:
        for url in ncfg.get("sale_pages", []):
            try:
                page = fetch(url, "text/html").decode("utf-8", "ignore")
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
                data = json.loads(m.group(1))
            except Exception as e:
                print(f"  ! nike page {url} failed: {e}")
                continue
            for p in _nike_products(data):
                pr = p["prices"]
                now, was = pr.get("currentPrice"), pr.get("initialPrice") or 0
                off = pr.get("discountPercentage") or 0
                cat = type_to_cat.get(p.get("productType"))
                if not now or off < min_off or not cat or p["productCode"] in done:
                    continue
                done.add(p["productCode"])
                c = p.get("copy", {})
                img = (p.get("colorwayImages") or {}).get("squarishURL", "")
                link = (p.get("pdpUrl") or {}).get("url", "")
                color = (p.get("displayColors") or {}).get("colorDescription", "")
                out.setdefault(cat, []).append({
                    "id": "nike:" + p["productCode"],
                    "title": f"{c.get('title', '')} ({color}) - {c.get('subTitle', '')} ${now:g} (was ${was:g}, {off}% off)",
                    "link": link,
                    "image": img,
                    "thumbs": 0,
                    "price": f"${now:g}",
                    "details": f"{color} - nike.com price ${now:g}, regular ${was:g}",
                    "source": "nike.com",
                })
            time.sleep(2)
    return out


def schema_product(url, w):
    """Any store page that publishes standard schema.org product data (on.com etc.)."""
    only, max_price = w.get("only_color_code"), w.get("alert_below")
    sizes = [str(s) for s in w.get("sizes", [])]
    try:
        page = fetch(url, "text/html").decode("utf-8", "ignore")
    except Exception as e:
        print(f"  ! watch {url} failed: {e}")
        return []
    variants = []
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                if o.get("@type") == "Product" and o.get("offers"):
                    variants.append(o)
                stack.extend(v for k, v in o.items() if k != "offers")
    out, done = [], set()
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    for v in variants:
        sku = str(v.get("sku") or v.get("productID") or v.get("name"))
        if (only and sku != only) or sku in done:
            continue
        done.add(sku)
        offer = v["offers"][0] if isinstance(v["offers"], list) else v["offers"]
        try:
            now = float(offer.get("price") or offer.get("lowPrice"))
        except (TypeError, ValueError):
            continue
        if max_price and now > max_price:
            continue
        stock = "in stock" if "InStock" in str(offer.get("availability")) else "check stock"
        if sizes:
            # on.com embeds per-size stock as ..."10.5","<SKU> M 10.5",107,...
            per = {m[0]: int(m[1]) for m in re.findall(
                r'"[\d.]+","' + re.escape(sku) + r' [A-Z]+ ([\d.]+)",(\d+)', page)}
            if per:
                have = {s: per.get(s, 0) for s in sizes}
                if not any(have.values()):
                    print(f"  - {sku}: none of sizes {sizes} in stock, no alert")
                    continue
                stock = ", ".join(f"size {s}: {'%d left' % n if n else 'sold out'}"
                                  for s, n in have.items())
            else:
                stock += f"; sizes {'/'.join(sizes)}: open the link to confirm"
        img = v.get("image")
        img = img[0] if isinstance(img, list) else (img or "")
        out.append({
            "id": f"watch:{host}:{sku}",
            "title": f"{v.get('name', '')} ${now:g}",
            "link": offer.get("url") or url,
            "image": img,
            "thumbs": 0,
            "price": f"${now:g}",
            "details": f"Watched product - {host}. {stock}",
            "source": f"{host} (watched)",
        })
    if not out:
        print(f"  ! watch {url}: no product price found")
    return out


def watched_products(cfg):
    """Exact product pages you care about (every color). Alerts on price drops."""
    out = {}
    for w in cfg.get("watch_products", []):
        url, cat = w["url"], w.get("category", "Shoes")
        want = [str(s) for s in w.get("sizes", [])]
        max_price = w.get("alert_below")
        only = w.get("only_color_code")
        host = urllib.parse.urlparse(url).netloc.replace("www.", "")
        if any(host.endswith(b["domain"]) for b in
               cfg.get("google_shopping", {}).get("brands", {}).values()):
            continue  # blocked brand: the google-shopping worker checks this one
        if "nike.com" not in url:
            items = schema_product(url, w)
            if items:
                out.setdefault(cat, []).extend(items)
            continue
        try:
            page = fetch(url, "text/html").decode("utf-8", "ignore")
            data = json.loads(re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1))
            groups = data["props"]["pageProps"]["productGroups"]
        except Exception as e:
            print(f"  ! watch {url} failed: {e}")
            continue
        for g in groups:
            for sc, o in (g.get("products") or {}).items():
                if only and sc != only:
                    continue
                pr = o.get("prices") or {}
                now, was = pr.get("currentPrice"), pr.get("initialPrice") or 0
                if not now or (max_price and now > max_price):
                    continue
                listed = {s["label"] for s in o.get("sizes", [])
                          if s.get("status", "ACTIVE") == "ACTIVE"}
                mine = [s for s in want if s in listed]
                if want and listed and not mine:
                    continue  # none of your sizes offered in this color
                info = o.get("productInfo") or {}
                ci = o.get("contentImages") or [{}]
                img = (((ci[0] or {}).get("properties") or {}).get("squarish") or {}).get("url", "")
                off = pr.get("discountPercentage") or 0
                out.setdefault(cat, []).append({
                    "id": "watch:" + sc,
                    "title": f"{info.get('fullTitle', '')} ({o.get('colorDescription', '')}) ${now:g}"
                             + (f" (was ${was:g}, {off}% off)" if was > now else ""),
                    "link": (o.get("pdpUrl") or {}).get("url", url),
                    "image": img,
                    "thumbs": 0,
                    "price": f"${now:g}",
                    "details": (f"Watched product - nike.com. Sizes {'/'.join(mine)} listed for this color "
                                f"(Nike doesn't show live stock - open the link to confirm).") if want
                               else "Watched product - nike.com",
                    "source": "nike.com (watched)",
                })
        time.sleep(2)
    return out


# ---------------------------------------------------------------- Google Shopping (SerpApi)
DIRECT_DOMAINS = ("nike.com", "on.com")  # these we read straight from the brand site


def _query_from_url(url, brand):
    """hoka.com/en/us/mens-road/clifton-11/1176572.html -> 'HOKA clifton 11 men's'"""
    parts = [re.sub(r"\.html?$", "", p) for p in urllib.parse.urlparse(url).path.split("/") if p]
    skip = {"en", "us", "en_us", "en-us", "products", "product", "p", "t", "shop"}
    words = [p for p in parts if len(re.findall(r"[a-z]", p, re.I)) >= 3
             and len(re.findall(r"[a-z]", p, re.I)) / len(p) > 0.5  # skip codes like ANA_1011B958
             and p.lower() not in skip and not re.match(r"^(mens|womens|men|women)(-[a-z]+)?$", p, re.I)]
    # the product name is usually the last word-y segment (category comes before it)
    name = words[-1] if words else ""
    name = re.sub(r"[-_]+", " ", name)
    name = re.sub(r"\b(mens?|womens?|shoes?)\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip()
    return f"{brand} {name} men's".strip()


def google_shopping_prices(cfg, state):
    """For favorites on brands that block us: look the product up on Google
    Shopping (via SerpApi) and read the price the BRAND'S OWN STORE is listing.
    Budget-friendly: each product is checked at most every `hours_between_checks`."""
    gc = cfg.get("google_shopping", {})
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        print("  ! SERPAPI_KEY not set - google worker skipped")
        return {}
    every = gc.get("hours_between_checks", 8) * 3600
    last = state.setdefault("google_last_check", {})
    now_t = int(time.time())
    out = []
    for w in cfg.get("watch_products", []):
        url = w["url"]
        host = urllib.parse.urlparse(url).netloc.replace("www.", "")
        if any(host.endswith(d) for d in DIRECT_DOMAINS):
            continue
        brand_cfg = next((b for b in gc.get("brands", {}).values()
                          if host.endswith(b["domain"])), None)
        if not brand_cfg:
            print(f"  ! {host}: add it under google_shopping.brands to price-check it")
            continue
        q = w.get("search") or _query_from_url(url, brand_cfg["name"])
        if now_t - last.get(url, 0) < every:
            continue
        last[url] = now_t
        api = ("https://serpapi.com/search.json?engine=google_shopping&gl=us&hl=en&num=40&q="
               + urllib.parse.quote_plus(q) + "&api_key=" + urllib.parse.quote_plus(key))
        try:
            res = json.loads(fetch(api, "application/json"))
        except Exception as e:
            print(f"  ! google shopping '{q}' failed: {e}")
            continue
        sellers = [s.lower() for s in brand_cfg.get("seller_names", [])] + [brand_cfg["domain"]]
        mine = [r for r in res.get("shopping_results", [])
                if any(s in (r.get("source") or "").lower() for s in sellers)
                and r.get("extracted_price")]
        if not mine:
            print(f"  - '{q}': brand store not in Google Shopping results right now")
            continue
        best = min(mine, key=lambda r: r["extracted_price"])
        p, was = best["extracted_price"], best.get("extracted_old_price")
        out.append({
            "id": f"gshop:{url}",
            "title": f"{best.get('title', q)} ${p:g}" + (f" (was ${was:g})" if was else "")
                     + f" - sold by {best.get('source')}",
            "link": url,
            "link_label": f"Open on {host}",
            "extra_links": [("Google Shopping listing", best.get("product_link") or best.get("link") or "")],
            "image": best.get("thumbnail", ""),
            "thumbs": 0,
            "price": f"${p:g}",
            "details": (f"Price listed by {best.get('source')} on Google Shopping (the brand's own store). "
                        f"Sizes {'/'.join(map(str, w.get('sizes', [])))}: open the link to confirm."),
            "source": "Google Shopping",
        })
        time.sleep(1)
    return {"Favorites": out} if out else {}


# ---------------------------------------------------------------- Brand emails (Gmail)
def brand_emails(cfg, state):
    """Option 3: read the brands' OWN sale / price-drop emails in your Gmail.
    Labels them 'Brand Deals' and sends you an alert when one is a price drop /
    sale, or mentions one of your favorite shoes."""
    import email as email_lib
    import imaplib
    from email.header import decode_header, make_header
    ec = cfg.get("brand_emails", {})
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        print("  ! Gmail secrets not set - brand-emails worker skipped")
        return {}
    keywords = [k.lower() for k in ec.get("alert_keywords", [])]
    favs = [k.lower() for k in ec.get("favorite_names", [])]
    label = ec.get("label", "Brand Deals")
    done = state.setdefault("emails_done", {})
    out = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw)
    M.select("INBOX")
    try:
        M.create(f'"{label}"')
    except Exception:
        pass
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 2 * 86400))
    for brand, domain in ec.get("senders", {}).items():
        typ, data = M.search(None, f'(SINCE {since} FROM "{domain}")')
        for num in (data[0].split() if typ == "OK" else []):
            typ, msg_data = M.fetch(num, "(BODY.PEEK[])")  # PEEK = don't mark as read
            msg = email_lib.message_from_bytes(msg_data[0][1])
            mid = msg.get("Message-ID", f"{domain}:{num.decode()}")
            if mid in done:
                continue
            done[mid] = int(time.time())
            M.copy(num, f'"{label}"')
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            body = ""
            for part in msg.walk():
                if part.get_content_type() in ("text/html", "text/plain"):
                    try:
                        body += part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", "ignore")
                    except Exception:
                        pass
            text = (subject + " " + re.sub(r"<[^>]+>", " ", body)).lower()
            hit_fav = [f for f in favs if f in text]
            hit_kw = [k for k in keywords if k in text]
            if not (hit_fav or hit_kw):
                continue
            img = re.search(r'<img[^>]+src="(https://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', body, re.I)
            link = re.search(r'href="(https://[^"]*' + re.escape(domain) + r'[^"]*)"', body)
            why = (f"mentions your favorite: {', '.join(hit_fav)}" if hit_fav
                   else f"sale email ({', '.join(hit_kw[:3])})")
            out.append({
                "id": f"mail:{mid}",
                "title": f"{brand} email - {subject}" + (" - YOUR FAVORITE!" if hit_fav else ""),
                "link": link.group(1) if link else f"https://www.{domain}",
                "link_label": f"Open on {domain}",
                "image": img.group(1) if img else "",
                "thumbs": 0,
                "price": "",
                "details": f"From {brand}'s own email ({why}). The full email is in your Gmail under '{label}'.",
                "source": f"{brand} email",
            })
    M.logout()
    cutoff = time.time() - 14 * 86400
    state["emails_done"] = {k: v for k, v in done.items() if v > cutoff}
    return {"Brand Email": out} if out else {}


# ---------------------------------------------------------------- Zappos
def _money(s):
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def zappos_brand_shoes(cfg, state, only=None):
    """Every color of every men's shoe per brand on Zappos (authorized retailer).
    Alerts when a color's price is lower than the last time we saw it
    (covers new markdowns and further drops). One email per shoe model."""
    zc = cfg.get("zappos", {})
    if not zc.get("brands"):
        return {}
    prices = state.setdefault("zappos_prices", {})
    cat = zc.get("category", "Brand Shoes")
    min_drop = zc.get("min_drop_percent", 0)
    models = {}  # (brand, productId) -> list of dropped colors
    for brand, b in zc["brands"].items():
        if only and brand != only:
            continue
        cat = brand
        names = [n.lower() for n in b.get("brand_names", [brand])]
        for page in range(zc.get("max_pages", 3)):
            term = urllib.parse.quote_plus(b["slug"].replace("-", " "))
            url = (f"https://www.zappos.com/{b['slug']}/" if page == 0 else
                   f"https://www.zappos.com/{b['slug']}/.zso?t={term}&p={page}")
            try:
                html_ = fetch(url, "text/html").decode("utf-8", "ignore")
                st = json.loads(re.search(
                    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", html_, re.S).group(1))
                plist = st["products"]["list"]
            except Exception as e:
                print(f"  ! zappos {brand} page {page} failed: {e}")
                break
            for p in plist:
                for s in (p.get("relatedStyles") or [p]):
                    if (s.get("brandName") or "").lower() not in names:
                        continue
                    if s.get("productType") and s["productType"] != "Shoes":
                        continue
                    now, orig = _money(s.get("price")), _money(s.get("originalPrice"))
                    if now is None:
                        continue
                    key = f"z:{s['styleId']}"
                    old = prices.get(key)
                    prices[key] = now
                    if old is None or now >= old:
                        continue
                    if (1 - now / old) * 100 < min_drop:
                        continue
                    models.setdefault((s["brandName"], s["productId"]), []).append(
                        (now, old, orig, s))
            if page + 1 >= (st.get("filters") or {}).get("pageCount", 1):
                break
            time.sleep(2)
        time.sleep(2)

    out = []
    for (brand, pid), drops in models.items():
        drops.sort(key=lambda d: d[0])
        now, old, orig, s = drops[0]
        img = (f"https://m.media-amazon.com/images/I/{s['msaImageId']}._AC_SR768,1024_.jpg"
               if s.get("msaImageId") else "")
        others = (f" + {len(drops) - 1} more color(s) dropped" if len(drops) > 1 else "")
        cfg_b = next((b for k, b in zc["brands"].items()
                      if brand.lower() in [n.lower() for n in b.get("brand_names", [k])]), {})
        query = urllib.parse.quote_plus(f"{s.get('productName', '')}")
        brand_link = cfg_b.get("brand_search", "").replace("{q}", query)
        domain = cfg_b.get("brand_domain", "")
        google = ("https://www.google.com/search?q=" +
                  urllib.parse.quote_plus(f"site:{domain} {brand} {s.get('productName', '')} men's"))
        out.append({
            "id": f"zap:{pid}:{old:g}->{now:g}:{int(time.time())}",  # every drop is its own alert
            "title": f"{brand} {s.get('productName', '')} ({s.get('styleColor', '')}) "
                     f"${now:g} (was ${old:g}) - Zappos price{others}",
            "link": brand_link or google,
            "link_label": f"Check on {domain or 'brand site'}",
            "extra_links": [("Search Google for it on " + domain, google),
                            ("See it on Zappos", "https://www.zappos.com" + (s.get("productUrl") or ""))],
            "image": img,
            "thumbs": 0,
            "price": f"${now:g}",
            "details": (f"HEADS-UP: this drop was seen on Zappos (${old:g} -> ${now:g}, regular ${orig:g}, "
                        f"{s.get('percentOff', '')} off). {domain or brand} blocks automatic checks, so the "
                        f"brand-site price is NOT confirmed - tap the button to check it there "
                        f"(brands usually run the same sale). Check size 10 / 10.5."),
            "source": "Zappos heads-up",
        })
    # your favorite models (e.g. FuelCell Rebel v5) also go out as [Favorites]
    favs = [m.lower() for m in zc.get("favorite_models", [])]
    result = {}
    fav_only = bool(only and zc["brands"].get(only, {}).get("only_favorites"))
    for item in out:
        is_fav = any(m in item["title"].lower() for m in favs)
        if fav_only and not is_fav:
            continue  # this worker only watches your favorite models
        if is_fav:
            item["title"] = "FAVORITE - " + item["title"]
        result.setdefault("Favorites" if is_fav else cat, []).append(item)
    return result


# ---------------------------------------------------------------- Shopify
def shopify_drops(cfg, state):
    """Products whose lowest variant price went down since last check."""
    prices = state.setdefault("shopify_prices", {})
    out = {}
    for cat, stores in cfg.get("shopify_stores", {}).items():
        if cat.startswith("_"):
            continue
        for store in stores:
            try:
                data = json.loads(fetch(f"https://{store}/products.json?limit=250",
                                        "application/json"))
            except Exception as e:
                print(f"  ! store {store} skipped: {e}")
                continue
            for p in data.get("products", []):
                vs = [v for v in p.get("variants", []) if v.get("price")]
                if not vs:
                    continue
                low = min(vs, key=lambda v: float(v["price"]))
                price = float(low["price"])
                was = float(low.get("compare_at_price") or 0)
                key = f"{store}:{p['id']}"
                old = prices.get(key)
                prices[key] = price
                if old is None or price >= old:
                    continue
                img = (p.get("images") or [{}])[0].get("src", "")
                ref = max(old, was)
                out.setdefault(cat, []).append({
                    "id": f"shop:{key}:{price}",
                    "title": f"{p['title']} now ${price:.2f} (was ${ref:.2f})",
                    "link": f"https://{store}/products/{p['handle']}",
                    "image": img,
                    "thumbs": 0,
                    "price": f"${price:.2f}",
                    "details": f"{round((1 - price / ref) * 100)}% off - {store}",
                    "source": store,
                })
            time.sleep(1)
    return out


# ---------------------------------------------------------------- Email
def build_email(cat, d):
    img = (f'<a href="{d["link"]}"><img src="{d["image"]}" width="300" '
           f'style="max-width:100%;border-radius:8px" alt=""></a><br>'
           if d["image"] else "")
    thumbs = f' &middot; 👍 {d["thumbs"]:+d}' if d["thumbs"] else ""
    body = f"""<div style="font-family:Arial,sans-serif;max-width:520px">
  <div style="font-size:12px;color:#888;text-transform:uppercase">{html.escape(cat)} deal &middot; {html.escape(d['source'])}{thumbs}</div>
  <h2 style="margin:6px 0 12px;font-size:18px">{html.escape(d['title'])}</h2>
  {img}
  <p style="color:#444">{html.escape(d['details'])}</p>
  <a href="{d['link']}" style="display:inline-block;background:#e4002b;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:bold">{html.escape(d.get('link_label', 'View deal'))}</a>
  <p style="font-size:12px;color:#888">{d['link']}</p>
  {''.join(f'<p style="font-size:13px"><a href="{u}">{html.escape(t)}</a></p>' for t, u in d.get('extra_links', []))}
</div>"""
    subject = f"[{cat}] {d['price'] + ' - ' if d['price'] else ''}{d['title']}"[:180]
    return subject, body


def send_all(emails):
    if os.environ.get("DRY_RUN") == "1":
        for s, _ in emails:
            print("  DRY RUN would send:", s)
        return len(emails)
    user = os.environ["GMAIL_USER"]
    to = os.environ.get("ALERT_TO") or user
    sent = 0
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, os.environ["GMAIL_APP_PASSWORD"])
        for subject, body in emails:
            msg = MIMEMultipart("alternative")
            msg["Subject"], msg["From"], msg["To"] = subject, user, to
            msg.attach(MIMEText(body, "html", "utf-8"))
            smtp.sendmail(user, [to], msg.as_string())
            sent += 1
            time.sleep(1)
    return sent


# ---------------------------------------------------------------- main
def workers(cfg):
    """Each worker runs on its own (own GitHub job, own state, own emails)."""
    w = {"favorites": lambda st: watched_products(cfg),
         "nike": lambda st: nike_deals(cfg),
         "google-shopping": lambda st: google_shopping_prices(cfg, st),
         "brand-emails": lambda st: brand_emails(cfg, st)}
    for brand in cfg.get("zappos", {}).get("brands", {}):
        w[brand.lower().replace(" ", "-")] = (
            lambda st, b=brand: zappos_brand_shoes(cfg, st, only=b))
    return w


def main():
    cfg = load_json(CONFIG_FILE, None)
    if not cfg:
        sys.exit("config.json missing or invalid")
    all_workers = workers(cfg)
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in all_workers:
        sys.exit(f"usage: python deal_alerts.py <worker>\nworkers: {', '.join(all_workers)}")
    state_file = os.path.join(HERE, f"state-{name}.json")
    state = load_json(state_file, {})
    first_run = "seen" not in state
    seen = state.setdefault("seen", {})
    now = int(time.time())
    print(f"worker: {name}")

    found = {}
    sources = list(all_workers[name](state).items())
    for cat, items in sources:
        found.setdefault(cat, []).extend(items)

    # price-drop tracking: EVERY drop vs the last price we saw gets an alert
    # (200 -> 180 -> 150 -> 120 = 3 alerts; 180 -> 200 -> 180 = alert again).
    # Rises are not alerted, but remembered, so the next drop is caught.
    last_price = state.setdefault("last_price", {})
    lowest = state.setdefault("lowest_price", {})
    min_step = cfg.get("min_drop_dollars", 0.5)  # ignore cent-level wobbles
    new, price_updates = [], {}
    for cat, items in found.items():
        for d in items:
            p = price_num(d["price"])
            old = last_price.get(d["id"])
            if p is not None:
                price_updates[d["id"]] = p
            if d["id"] not in seen:
                new.append((cat, d))
            elif p is not None and old is not None and p <= old - min_step:
                low = min(lowest.get(d["id"], old), old)
                drop = dict(d)
                drop["id"] = f"{d['id']}:drop:{old:g}->{p:g}:{now}"
                drop["base_id"] = d["id"]
                drop["title"] = f"PRICE DROP ${old:g} -> ${p:g}: {d['title']}"
                record = ("NEW LOWEST price we've seen!" if p < low
                          else f"Lowest we've seen: ${low:g}.")
                drop["details"] = (f"Down ${old - p:g} ({round((1 - p / old) * 100)}%) since the last check. "
                                   f"{record} " + d["details"])
                new.append((cat, drop))
    print(f"found {sum(len(v) for v in found.values())} matching deals, {len(new)} new")

    cap = cfg.get("max_emails_per_run", 15)
    to_send = [] if first_run else new[:cap]
    if first_run:
        print("first run: recording current deals, no emails sent")
    elif len(new) > cap:
        print(f"capping at {cap} emails; {len(new) - cap} more will go next run")

    emails = [build_email(cat, d) for cat, d in to_send]
    sent = send_all(emails) if emails else 0

    # mark as seen: everything on first run, otherwise only what was sent
    for cat, d in (new if first_run else to_send[:sent]):
        seen[d["id"]] = now
    # remember prices - except drops we could not send yet (cap), so the
    # next run still sees them as drops and emails them
    unsent = {d.get("base_id") for _, d in new[len(to_send[:sent]):]} if not first_run else set()
    for k, p in price_updates.items():
        if k in unsent:
            continue
        last_price[k] = p
        lowest[k] = min(lowest.get(k, p), p)
    cutoff = now - STATE_KEEP_DAYS * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    live = {k.split(":drop:")[0] for k in state["seen"]}
    state["last_price"] = {k: v for k, v in last_price.items() if k in live}
    state["lowest_price"] = {k: v for k, v in lowest.items() if k in live}
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0)
    print(f"sent {sent} email(s)")


if __name__ == "__main__":
    main()
