"""Signal collectors. All of these cost ZERO LLM tokens.

free collectors: website, ATS job boards, Wayback, Hacker News, Reddit, Wikipedia
key collectors:  Google Places (GOOGLE_PLACES_API_KEY), SerpAPI (SERPAPI_API_KEY)

Each collector returns {signal_id: value} plus a raw dict for the audit trail, and
never raises on network failure - missing data simply lowers coverage.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import date, timedelta
from urllib.parse import urlparse

from .http import Http, html_to_text
from .scoring import bayesian_rating

SOCIAL_PATTERNS = {
    "tiktok": r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+",
    "instagram": r"https?://(?:www\.)?instagram\.com/[\w.\-]+",
    "youtube": r"https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)[\w.\-]+",
    "facebook": r"https?://(?:www\.)?facebook\.com/[\w.\-]+",
    "linkedin": r"https?://(?:www\.)?linkedin\.com/company/[\w.\-]+",
    "x": r"https?://(?:www\.)?(?:twitter|x)\.com/[\w]+",
}
ATS_PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w\-]+)",
    "lever": r"jobs\.lever\.co/([\w\-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([\w\-]+)",
    "workable": r"apply\.workable\.com/([\w\-]+)",
}
FINANCE_ROLE = re.compile(r"\b(cfo|chief financial|controller|fp&a|vp,? finance|head of finance|director of finance)\b", re.I)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or None


# ---------------------------------------------------------------- website
def website(http: Http, url: str) -> tuple[dict, dict]:
    d = domain_of(url)
    if not d:
        return {}, {}
    source_url = f"https://{d}/"
    status, html = http.get(source_url)
    if status != 200:
        source_url = f"https://www.{d}/"
        status, html = http.get(source_url)
    if status != 200 or not html:
        return {}, {"error": f"homepage status {status}"}
    raw: dict = {"socials": {}, "ats": None, "source_url": source_url}
    for k, pat in SOCIAL_PATTERNS.items():
        m = re.search(pat, html)
        if m:
            raw["socials"][k] = m.group(0).rstrip("/")
    for k, pat in ATS_PATTERNS.items():
        m = re.search(pat, html)
        if m:
            raw["ats"] = (k, m.group(1))
            break
    # schema.org JSON-LD: address & aggregateRating if the site publishes them
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            ar = item.get("aggregateRating")
            if isinstance(ar, dict):
                raw["site_rating"] = (ar.get("ratingValue"), ar.get("reviewCount") or ar.get("ratingCount"))
            addr = item.get("address")
            if isinstance(addr, dict):
                raw["address"] = {k: addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")}
    loc_links = set(re.findall(r'href="([^"]*/locations?/[^"#?]+)"', html, re.I))
    raw["location_links"] = len(loc_links)
    raw["text"] = html_to_text(html, 2500)
    raw["has_careers"] = bool(re.search(r'href="[^"]*(careers|jobs|join-us|work-with-us)', html, re.I))
    return {}, raw


# ---------------------------------------------------------------- ATS job boards
def jobs(http: Http, ats: tuple[str, str] | None) -> tuple[dict, dict]:
    if not ats:
        return {}, {}
    kind, token = ats
    boards = {
        "greenhouse": (f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", "jobs", "title"),
        "lever": (f"https://api.lever.co/v0/postings/{token}?mode=json", None, "text"),
        "ashby": (f"https://api.ashbyhq.com/posting-api/job-board/{token}", "jobs", "title"),
    }
    if kind not in boards:
        return {}, {"ats": kind}
    url, key, title_key = boards[kind]
    j = http.json(url)
    listings = j.get(key) if key and isinstance(j, dict) else j
    if not isinstance(listings, list):
        return {}, {"ats": kind}
    titles = [x.get(title_key, "") for x in listings]
    fin = [t for t in titles if FINANCE_ROLE.search(t)]
    return ({"open_roles": len(titles), "finance_hire": bool(fin)},
            {"ats": kind, "roles": titles[:40], "finance_roles": fin, "source_url": url})


# ---------------------------------------------------------------- Wayback Machine
def wayback(http: Http, url: str) -> tuple[dict, dict]:
    d = domain_of(url)
    if not d:
        return {}, {}
    url = f"https://web.archive.org/cdx/search/cdx?url={d}&output=json&fl=timestamp&collapse=timestamp:6&limit=2000"
    j = http.json(url, cache_days=30, timeout=30)
    if not j or len(j) < 2:
        return {}, {}
    years: dict[int, int] = {}
    for row in j[1:]:
        y = int(row[0][:4])
        years[y] = years.get(y, 0) + 1
    first = min(years)
    this = time.gmtime().tm_year
    recent, prior = years.get(this - 1, 0), years.get(this - 2, 0)
    vals = {}
    if prior:
        vals["site_expansion"] = round(recent / prior - 1, 3)
    return vals, {"first_archived": first, "months_archived_by_year": years,
                  "source_url": url, "period": f"{this - 2}/{this - 1}"}


# ---------------------------------------------------------------- community
def hackernews(http: Http, name: str) -> tuple[dict, dict]:
    q = urllib.parse.quote(f'"{name}"')
    url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=0"
    j = http.json(url)
    return ({}, {}) if not j else ({}, {"hn_hits": j.get("nbHits", 0), "source_url": url,
                                        "period": "All indexed stories at retrieval"})


def reddit(http: Http, name: str) -> tuple[dict, dict]:
    q = urllib.parse.quote(f'"{name}"')
    url = f"https://www.reddit.com/search.json?q={q}&limit=100&t=year&sort=new"
    j = http.json(url)
    if not j:
        return {}, {}
    posts = j.get("data", {}).get("children", [])
    subs: dict[str, int] = {}
    for p in posts:
        s = p.get("data", {}).get("subreddit", "")
        subs[s] = subs.get(s, 0) + 1
    today = date.today()
    return {}, {"reddit_posts_12m": len(posts), "top_subreddits": sorted(subs, key=subs.get, reverse=True)[:5],
                "source_url": url, "period": f"{today - timedelta(days=365)}/{today}"}


def wikipedia(http: Http, name: str) -> tuple[dict, dict]:
    q = urllib.parse.quote(name)
    url = f"https://en.wikipedia.org/w/api.php?action=query&titles={q}&format=json&redirects=1"
    j = http.json(url)
    if not j:
        return {}, {}
    pages = j.get("query", {}).get("pages", {})
    exists = any(int(k) > 0 for k in pages)
    return {"wikipedia_page": exists}, {"source_url": url, "page_ids": list(pages)}


# ---------------------------------------------------------------- key-based
def google_places(http: Http, key: str | None, name: str, city: str | None) -> tuple[dict, dict]:
    """Aggregates rating & review count across a brand's locations (Places API New)."""
    if not key:
        return {}, {}
    body = json.dumps({"textQuery": f"{name} {city or ''}".strip(), "pageSize": 20}).encode()
    url = "https://places.googleapis.com/v1/places:searchText"
    status, text = http.get(url, data=body, headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "places.displayName,places.rating,places.userRatingCount,places.formattedAddress"})
    if status != 200:
        return {}, {"places_error": status}
    places = json.loads(text).get("places", [])
    token = name.lower().split()[0]
    mine = [p for p in places if token in p.get("displayName", {}).get("text", "").lower()]
    if not mine:
        return {}, {"places_found": 0, "source_url": url, "query": json.loads(body)["textQuery"]}
    cnt = sum(p.get("userRatingCount", 0) for p in mine)
    rating = sum(p.get("rating", 0) * p.get("userRatingCount", 0) for p in mine) / max(cnt, 1)
    return ({"review_rating": round(bayesian_rating(rating, cnt), 3), "review_count": cnt},
            {"places_found": len(mine), "places_raw_rating": round(rating, 2), "source_url": url,
             "query": json.loads(body)["textQuery"],
             "addresses": [p.get("formattedAddress") for p in mine][:20]})


def serp_rank(http: Http, key: str | None, domain: str | None, keywords: list[str]) -> tuple[dict, dict]:
    if not key or not domain or not keywords:
        return {}, {}
    ranks, local_hit, urls = {}, False, []
    for kw in keywords[:3]:  # cap: each query costs the user SerpAPI credit
        q = urllib.parse.urlencode({"engine": "google", "q": kw, "num": 30})
        url = f"https://serpapi.com/search.json?{q}"
        j = http.json(f"{url}&api_key={urllib.parse.quote(key, safe='')}", cache_days=7)
        if not j:
            continue
        pos = next((r["position"] for r in j.get("organic_results", []) if domain in r.get("link", "")), None)
        ranks[kw] = pos or 31
        urls.append(url)
        local_hit |= any(domain in (p.get("website") or "") for p in j.get("local_results", {}).get("places", [])
                         if isinstance(j.get("local_results"), dict))
    if not ranks:
        return {}, {}
    return ({"google_rank": sum(ranks.values()) / len(ranks), "local_pack": local_hit},
            {"serp_ranks": ranks, "source_url": urls[0], "request_urls": urls})


def trends(http: Http, key: str | None, name: str) -> tuple[dict, dict]:
    """12-month Google Trends slope via SerpAPI: (last quarter avg / first quarter avg) - 1."""
    if not key:
        return {}, {}
    q = urllib.parse.urlencode({"engine": "google_trends", "q": name, "date": "today 12-m", "geo": "US"})
    url = f"https://serpapi.com/search.json?{q}"
    j = http.json(f"{url}&api_key={urllib.parse.quote(key, safe='')}", cache_days=7)
    try:
        timeline = j["interest_over_time"]["timeline_data"]
        series = [pt["values"][0]["extracted_value"] for pt in timeline]
    except (TypeError, KeyError, IndexError):
        return {}, {}
    if len(series) < 8:
        return {}, {}
    first, last = sum(series[:13]) / 13, sum(series[-13:]) / 13
    raw = {"trend_series": series, "source_url": url,
           "period": f"{timeline[0].get('date', 'start')}/{timeline[-1].get('date', 'end')} (last 12 months)"}
    return ({"search_interest_trend": round(last / first - 1, 3)} if first > 0 else {}), raw
