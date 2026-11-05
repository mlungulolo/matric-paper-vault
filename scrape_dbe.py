#!/usr/bin/env python3
"""
Collect official NSC past-paper LINKS from the Department of Basic Education
(education.gov.za) and write them to papers_auto.json for build.py.

We store links only. We do not download or re-host any paper.

Usage:
  python3 scrape_dbe.py                      # discover year pages and scrape all
  python3 scrape_dbe.py --only 2025          # only pages whose URL/title has 2025
  python3 scrape_dbe.py --page URL           # scrape one specific page
  python3 scrape_dbe.py --html saved.html --year 2025 --sitting Nov   # parse a saved file

Be polite: it waits between requests, caches pages in ./cache, checks robots.txt,
and identifies itself. Read the DBE site's terms of use before running it often.

HOW IT WORKS (so you can fix it when the DBE site changes)
  1. discover_pages()  finds the year/exam pages from the DBE index page.
  2. get()             downloads each page politely (delay, cache, robots.txt).
  3. parse_page()      walks the HTML in order. Each <h2>/<h3> is the subject
                       heading; each LinkClick.aspx link inside a table is a file.
  4. classify()        turns a title like "Paper 1 (English)" or "English HL P2 memo"
                       into subject + paper number + language + paper/memo/extra.
  5. merge()           groups rows by subject and flags suspicious ones.
  6. main()            writes papers_auto.json (and a CSV with --csv).

BEST PRACTICES
  * Links only. Never download or re-host the PDFs.
  * Keep the delay at 3 seconds or more, and run it weekly, not constantly.
  * If a run finds 0 rows, the site layout changed: open the saved page in ./cache
    and adjust parse_page(). Do not overwrite good data with an empty result
    (the script refuses to).
  * Treat scraped data as untrusted: it is escaped by build.py before display.
  * Spot-check a few links by hand after every big change.
"""
import argparse, csv, json, os, re, sys, time, hashlib
from urllib.parse import urljoin, urlparse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

# ===================== CONFIG =====================
CONTACT = "hello@yourdomain.co.za"            # put a real contact so the site owner can reach you
USER_AGENT = f"PastPaperLinkIndexer/1.0 (+{CONTACT})"
DELAY_SECONDS = 3
INDEX_URL = "https://www.education.gov.za/Curriculum/NationalSeniorCertificate%28NSC%29Examinations/NSCPastExaminationpapers.aspx"
# Known-good year page, used if discovery finds nothing:
SEED_PAGES = [
    "https://www.education.gov.za/Curriculum/NationalSeniorCertificate(NSC)Examinations/2025NovemberExamPapers.aspx",
]
OUT_FILE = "papers_auto.json"
CACHE_DIR = "cache"
# DBE heading name (lowercase) -> your site's subject slug (see SUBJECTS in build.py)
# Map the DBE heading to YOUR site's subject slug (must match SUBJECTS in build.py).
# Headings not listed here still get saved under a slug made from the heading.
SUBJECT_MAP = {
    "mathematics": "mathematics",
    "mathematical literacy": "mathematical-literacy",
    "physical sciences": "physical-sciences",
    "life sciences": "life-sciences",
    "accounting": "accounting",
    "geography": "geography",
    "history": "history",
    "business studies": "business-studies",
    "economics": "economics",
}
# ==================================================

# 'English HL' -> 'english-hl'. Used for URL-safe subject keys.
def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
_robots = {}

# Respect robots.txt: returns False if the site asks bots not to fetch this URL.
def allowed(url):
    host = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    if host not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            rp.set_url(host + "/robots.txt"); rp.read()
        except Exception:
            rp = None
        _robots[host] = rp
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)

# Polite download: uses the cache, checks robots.txt, retries 3 times, waits between requests.
def get(url, force=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".html")
    if os.path.exists(path) and not force:
        return open(path, encoding="utf-8").read()
    if not allowed(url):
        print(f"  robots.txt disallows {url}, skipping", file=sys.stderr); return None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                open(path, "w", encoding="utf-8").write(r.text)
                time.sleep(DELAY_SECONDS)
                return r.text
            print(f"  HTTP {r.status_code} for {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  error {e}", file=sys.stderr)
        time.sleep(DELAY_SECONDS * (attempt + 1))
    return None

# Finds all year/exam pages from the DBE index. Falls back to SEED_PAGES if discovery finds none.
def discover_pages():
    """Find year/exam pages linked from the DBE index page."""
    html = get(INDEX_URL)
    pages = {}
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(INDEX_URL, a["href"])
            text = a.get_text(" ", strip=True)
            if "NationalSeniorCertificate" in href and href.lower().endswith(".aspx"):
                if re.search(r"(19|20)\d\d", href + " " + text) and re.search(r"exam|paper|exemplar", href + " " + text, re.I):
                    pages[href] = text
    for u in SEED_PAGES:
        pages.setdefault(u, "")
    return list(pages)

# Reads the year and sitting (Nov, May/Jun, Feb/Mar, Exemplar) from the page title or URL.
def year_sitting(soup, url):
    title = (soup.title.get_text(" ", strip=True) if soup.title else "") + " " + url
    m = re.search(r"((?:19|20)\d\d)", title)
    year = int(m.group(1)) if m else 0
    t = title.lower()
    if "november" in t or "nov" in t: sitting = "Nov"
    elif "may" in t or "june" in t: sitting = "May/Jun"
    elif "feb" in t or "march" in t: sitting = "Feb/Mar"
    elif "exemplar" in t: sitting = "Exemplar"
    else: sitting = ""
    return year, sitting

LANG_RE = re.compile(r"^(?P<lang>[A-Za-z ]+?)\s+(?P<lvl>HL|FAL|SAL)\b", re.I)

# Core of the parser: converts one row title into (subject slug, pairing key, label, kind).
# kind is 'paper', 'memo' or 'extra' (answer book, addendum, data files).
# Edit the regular expressions here if DBE changes its naming.
def classify(heading, title):
    """Turn a DBE row title into (subject_slug, key, label, kind, extra_name)."""
    t = " ".join(title.split())
    low = t.lower()
    memo = bool(re.search(r"\bmemo", low))
    answerbook = bool(re.search(r"answer\s*book", low))
    addendum = "addendum" in low
    datafiles = "data files" in low
    # Language subjects: "English HL P1", "Afrikaans SAL P2 (Gauteng) memo"
    lm = LANG_RE.match(t)
    if lm:
        lang, lvl = lm.group("lang").strip(), lm.group("lvl").upper()
        pm = re.search(r"\bP\s?(\d)\b", t, re.I)
        num = pm.group(1) if pm else "?"
        rest = t[lm.end():]
        rest = re.sub(r"\bP\s?\d\b", "", rest, flags=re.I)
        rest = re.sub(r"\bmemo\b", "", rest, flags=re.I)
        variant = re.sub(r"[()]", "", rest).strip()
        names = {"HL": "home-language", "FAL": "first-additional-language", "SAL": "second-additional-language"}
        slug = f"{slugify(lang)}-{names[lvl]}"
        label = f"Paper {num}" + (f" ({variant})" if variant else "")
        return slug, (slug, num, variant), label, "memo" if memo else "paper", None
    # Other subjects
    slug = SUBJECT_MAP.get(heading.lower().strip(), slugify(heading))
    lang_m = re.search(r"\((English|Afrikaans)\)\s*$", t, re.I)
    language = lang_m.group(1).title() if lang_m else ""
    core = re.sub(r"\((English|Afrikaans)\)\s*$", "", t, flags=re.I).strip()
    nm = re.search(r"(?:paper|memo)\s*(\d)", core, re.I)
    num = nm.group(1) if nm else ""
    prefix = ""
    pm = re.match(r"^(.*?)\b(?:paper|memo)\b", core, re.I)
    if pm: prefix = pm.group(1).strip()
    comp = prefix
    if answerbook or addendum or datafiles or re.search(r"question\s*\d", low):
        return slug, (slug, comp, num, language), None, "extra", core
    base = f"Paper {num}" if num else core
    label = (f"{comp} " if comp else "") + base + (f" ({language})" if language else "")
    return slug, (slug, comp, num, language), label.strip(), "memo" if memo else "paper", None

# Parses one DBE page into rows. Duplicate titles are skipped; paper+memo pairs are joined into one row.
def parse_page(html, url, year=None, sitting=None):
    soup = BeautifulSoup(html, "html.parser")
    y, s = year_sitting(soup, url)
    year = year or y; sitting = sitting if sitting is not None else s
    heading = ""
    rows = {}
    order = []
    seen_titles = set()
    for el in soup.find_all(["h1", "h2", "h3", "h4", "a"]):
        if el.name != "a":
            heading = el.get_text(" ", strip=True)
            continue
        href = el.get("href", "")
        if "LinkClick.aspx" not in href or "forcedownload" in href: continue
        if not el.find_parent("table"): continue
        title = el.get_text(" ", strip=True)
        if not title or heading.upper() in ("LANGUAGES", "NON LANGUAGES", ""): continue
        link = urljoin(url, href)
        slug, key, label, kind, extra_name = classify(heading, title)
        tkey = (slug, title.lower())
        if tkey in seen_titles: continue          # duplicate row on the page
        seen_titles.add(tkey)
        if kind == "extra":
            base_key = key
            row = rows.setdefault(base_key, {"slug": slug, "year": year, "sitting": sitting, "label": None, "qp": "", "memo": "", "extras": []})
            row["extras"].append({"name": extra_name, "url": link})
            if row["label"] is None:
                comp, num, language = key[1], key[2], key[3]
                row["label"] = ((comp + " ") if comp else "") + (f"Paper {num}" if num else "Extras") + (f" ({language})" if language else "")
            if base_key not in order: order.append(base_key)
            continue
        row = rows.setdefault(key, {"slug": slug, "year": year, "sitting": sitting, "label": label, "qp": "", "memo": "", "extras": []})
        if row["label"] is None: row["label"] = label
        field = "memo" if kind == "memo" else "qp"
        if row[field] and row[field] != link:
            row.setdefault("_conflict", []).append(title)
        elif not row[field]:
            row[field] = link
        if key not in order: order.append(key)
    return [rows[k] for k in order]

# Groups rows by subject and flags rows whose paper and memo link are identical (a common DBE page error).
def merge(all_rows):
    out = {}
    for r in all_rows:
        if r["qp"] and r["qp"] == r["memo"]:       # DBE page sometimes lists the same file twice
            r.setdefault("_conflict", []).append("paper and memo link are identical")
        slug = r.pop("slug")
        out.setdefault(slug, []).append(r)
    # drop identical links used for different papers (DBE page typos) from being trusted silently
    return out

# Command line entry point (see the usage text at the top of this file).
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="only pages containing this text, e.g. 2025")
    ap.add_argument("--page", help="scrape a single page URL")
    ap.add_argument("--html", help="parse a saved HTML file instead of fetching")
    ap.add_argument("--year", type=int); ap.add_argument("--sitting")
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--csv", help="also write a spreadsheet-friendly CSV, e.g. --csv papers_links.csv")
    a = ap.parse_args()

    all_rows = []
    if a.html:
        html = open(a.html, encoding="utf-8").read()
        all_rows += parse_page(html, a.page or "https://www.education.gov.za/", a.year, a.sitting)
    else:
        pages = [a.page] if a.page else discover_pages()
        if a.only: pages = [p for p in pages if a.only in p]
        print(f"Scraping {len(pages)} page(s)")
        for p in pages:
            print("->", p)
            html = get(p, force=a.refresh)
            if not html: continue
            rows = parse_page(html, p)
            print(f"   {len(rows)} paper rows")
            all_rows += rows
    data = merge(all_rows)
    if not data:                                  # safety: never overwrite good data with nothing
        sys.exit("Found 0 rows: the page layout may have changed. Existing file left untouched.")
    data["_generated"] = [{"note": "Auto-generated by scrape_dbe.py. Do not edit by hand; use papers.json for manual links."}]
    # keep only keys build.py can use plus extras; write file
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    if a.csv:
        with open(a.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["subject", "year", "sitting", "paper", "question_paper_url", "memo_url", "extras"])
            for subj, rows in data.items():
                if subj.startswith("_"): continue
                for r in rows:
                    ex = " | ".join(f'{e["name"]}: {e["url"]}' for e in r.get("extras", []))
                    w.writerow([subj, r["year"], r["sitting"], r["label"], r["qp"], r["memo"], ex])
        print("Wrote CSV:", a.csv)
    n = sum(len(v) for k, v in data.items() if not k.startswith("_"))
    print(f"Wrote {n} rows across {len(data)-1} subjects to {a.out}")
    for k, v in data.items():
        if k.startswith("_"): continue
        c = sum(1 for r in v if r.get("_conflict"))
        if c: print(f"  warning: {k} has {c} row(s) with conflicting duplicate links; check them")

if __name__ == "__main__":
    main()
