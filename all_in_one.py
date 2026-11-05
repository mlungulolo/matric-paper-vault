#!/usr/bin/env python3
"""
all_in_one.py : the WHOLE project in a single file
==================================================
Scrapes official NSC past-paper LINKS from the DBE site AND builds your website.

USAGE
  python3 all_in_one.py scrape            collect links  -> papers_auto.json
  python3 all_in_one.py scrape --csv papers_links.csv   (also write a spreadsheet)
  python3 all_in_one.py build             build the website into ./dist
  python3 all_in_one.py all               scrape, then build

  Extra scrape options:  --only 2025   --page URL   --html saved.html --year 2025 --sitting Nov

SETUP
  1. Edit the config values in the two CONFIG blocks below
     (CONTACT / scraper settings, and SITE_NAME / DOMAIN / CONTACT_EMAIL / ADSENSE_CLIENT).
  2. Cloudflare Pages: build command  python3 all_in_one.py build   output dir  dist
  3. Scraping needs:  pip install requests beautifulsoup4   (building needs nothing extra)

This file is the scraper (scrape_dbe.py) and the site builder (build.py) joined
together. Read the comments in each section; they explain how and why.
BEST PRACTICES: link to official papers only, write original page text, keep the
scraper polite (3s delay, weekly), spot-check links, keep the not-affiliated notice.
"""
import argparse, csv, json, os, re, sys, time, hashlib, shutil, datetime, html
from urllib.parse import urljoin, urlparse
from urllib import robotparser
try:                                   # only needed for scraping; building works without them
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = BeautifulSoup = None

# ############################################################################
# SECTION 1 OF 3 : LINK SCRAPER (parses official DBE pages into papers_auto.json)
# ############################################################################


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

session = requests.Session() if requests else None
if session: session.headers["User-Agent"] = USER_AGENT
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
def scrape_main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="only pages containing this text, e.g. 2025")
    ap.add_argument("--page", help="scrape a single page URL")
    ap.add_argument("--html", help="parse a saved HTML file instead of fetching")
    ap.add_argument("--year", type=int); ap.add_argument("--sitting")
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--csv", help="also write a spreadsheet-friendly CSV, e.g. --csv papers_links.csv")
    if requests is None or BeautifulSoup is None:
        sys.exit("Scraping needs:  pip install requests beautifulsoup4")
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



# ############################################################################
# SECTION 2 OF 3 : WEBSITE BUILDER (turns the data files into the finished site)
# ############################################################################

# ===================== CONFIG: EDIT THESE =====================
SITE_NAME = "Matric Paper Vault"          # change to your chosen name
DOMAIN = "https://www.yourdomain.co.za"   # no trailing slash. Change once you own the domain
CONTACT_EMAIL = "hello@yourdomain.co.za"
ADSENSE_CLIENT = ""                       # e.g. "ca-pub-1234567890123456" (leave empty until approved)
# ==============================================================

DBE_PAGE = "https://www.education.gov.za/Curriculum/NationalSeniorCertificate(NSC)Examinations/NSCPastExaminationpapers.aspx"
TODAY = datetime.date.today().isoformat()

# SUBJECTS: one dictionary per subject page. 'slug' becomes the URL (/mathematics.html).
# Keep slugs lowercase with hyphens, and identical to the keys used in papers_auto.json / papers.json.
SUBJECTS = [
 {"slug":"mathematics","name":"Mathematics","papers":["Paper 1: algebra, sequences, functions, finance, calculus, probability","Paper 2: statistics, analytical geometry, trigonometry, Euclidean geometry"],
  "focus":["Calculus and functions carry heavy marks in Paper 1, so practise them first.","Euclidean geometry proofs reward neat reasoning: write a reason next to every statement.","Learn the formula sheet before test day so you know what you do not have to memorise.","Redo any question you got wrong a week later without looking at the memo."],
  "tip":"Work one full paper under timed conditions each week, then mark it strictly with the memo and list every lost mark by topic."},
 {"slug":"mathematical-literacy","name":"Mathematical Literacy","papers":["Paper 1: basic skills topics","Paper 2: applications and context-based problems"],
  "focus":["Finance questions (tax, interest, budgets) appear every year, so practise them until they feel routine.","Read the whole context before touching your calculator.","Map and plan questions need careful scale work, so measure twice.","Show your working: method marks matter even when the final answer is off."],
  "tip":"Pick one topic per day, do two past questions on it, then check the memo for how marks are awarded."},
 {"slug":"physical-sciences","name":"Physical Sciences","papers":["Paper 1: Physics","Paper 2: Chemistry"],
  "focus":["Learn the definitions word for word; they are easy marks in every paper.","Practise units and significant figures, because careless slips cost marks.","Draw a diagram for every mechanics and electricity question.","Balance equations and state the reaction type in organic chemistry questions."],
  "tip":"Keep a one-page list of definitions and laws and read it before every paper you practise."},
 {"slug":"life-sciences","name":"Life Sciences","papers":["Paper 1: life processes and environmental studies content","Paper 2: genetics, evolution and related content"],
  "focus":["Learn the terminology precisely; examiners mark for the exact term.","Practise reading graphs and tables, since data questions appear often.","Genetics crosses should be drawn in full: parents, gametes, offspring, ratio.","Essay questions reward a clear structure, so plan each one before writing."],
  "tip":"After each past paper, write a short summary of the topics where you lost marks and revise those first."},
 {"slug":"accounting","name":"Accounting","papers":["Typically one long paper covering financial statements, company accounts, cash flow, ratios, inventory and more. Check the cover page of each paper for time and marks"],
  "focus":["Practise full financial statements until the layout is automatic.","Ratio questions often ask for a comment as well as a calculation, so write a clear sentence.","Learn the standard adjustments and how each one affects the statements.","Present neat, well-labelled workings; they earn part marks."],
  "tip":"Time yourself on each question type separately, then combine them into full papers closer to the exam."},
 {"slug":"geography","name":"Geography","papers":["Paper 1: theory","Paper 2: mapwork and GIS skills"],
  "focus":["Mapwork is practice-heavy: calculate scale, gradient and area from real topographic maps.","Learn the processes behind each landform or climate feature, not just the names.","Use labelled diagrams in theory answers.","Know your settlement and economic geography case examples."],
  "tip":"Do one mapwork question every day; it builds speed faster than rereading notes."},
 {"slug":"history","name":"History","papers":["Paper 1 and Paper 2: source-based and essay questions across the Grade 12 topics"],
  "focus":["Practise source analysis: what the source says, who wrote it, why, and how useful it is.","Plan essays with a clear introduction, argument paragraphs and conclusion.","Keep a short timeline for each topic you study.","Back every claim with a specific piece of evidence."],
  "tip":"Write one timed essay a week and compare your structure with the memo's expected argument."},
 {"slug":"business-studies","name":"Business Studies","papers":["Paper 1 and Paper 2: short questions, case studies and essays"],
  "focus":["Learn definitions and lists, then practise applying them to the case study.","Link each answer to the scenario in the question, not a generic textbook answer.","Know the relevant legislation and what each Act is for.","Essay questions need headings and a clear structure."],
  "tip":"After reading a case study, underline the facts you can use before you start writing."},
 {"slug":"economics","name":"Economics","papers":["Paper 1: macroeconomics","Paper 2: microeconomics and related content"],
  "focus":["Draw graphs neatly and label every axis and curve.","Practise explaining shifts and movements in your own words.","Learn the key terms precisely.","For discussion questions, state your point, explain it, then give an example."],
  "tip":"Redraw each core graph from memory until you can do it without looking."},
 {"slug":"english-home-language","name":"English Home Language","papers":["Paper 1: language in context","Paper 2: literature","Paper 3: writing"],
  "focus":["Practise comprehension and summary writing under time pressure.","For literature, prepare short quotations and what each reveals about theme or character.","Plan your essay or transactional writing before drafting.","Proofread: spelling and punctuation matter in every paper."],
  "tip":"Write one full essay a week and ask a teacher or friend to mark it against the rubric."},
 {"slug":"english-first-additional-language","name":"English First Additional Language","papers":["Paper 1: language in context","Paper 2: literature","Paper 3: writing"],
  "focus":["Build your vocabulary with a few new words every day.","Practise the different transactional formats: letters, reports, reviews.","Know your set works: plot, characters, themes.","Read comprehension passages slowly, then re-read the questions."],
  "tip":"Read one short article a day and write two sentences summarising it."},
]

# PAGES_EXTRA: extra long-form articles. Add more helpful original articles here; they build trust and traffic.
PAGES_EXTRA = {
 "how-to-use-past-papers": {
  "title":"How to Use Past Papers to Study for Matric",
  "desc":"A practical method for using Grade 12 past papers and memos: when to start, how to mark yourself, and how to fix weak topics.",
  "body":"""
<p>Past papers are the closest thing you will get to the real exam. Used well, they show you how questions are asked, how marks are shared and where you are losing them. Used badly, they become a pile of unfinished PDFs. This is a method that works.</p>
<h2>1. Learn the content first</h2>
<p>Do not start with full papers on a topic you have not studied. Revise the section from your notes or textbook, then attempt past questions on that topic only.</p>
<h2>2. Do a paper under real conditions</h2>
<p>Once you have covered most of the syllabus, sit a full paper: same time limit, no phone, only what the exam allows. This builds stamina and shows you how you handle pressure.</p>
<h2>3. Mark yourself strictly</h2>
<p>Use the memorandum (memo) and give marks only where the memo does. Writing a mark next to every answer is slow but it teaches you what examiners accept.</p>
<h2>4. Track where marks are lost</h2>
<p>Make a simple table: topic, marks lost, reason. The reasons are usually one of three: you did not know it, you misread the question, or you made a careless mistake. Each needs a different fix.</p>
<h2>5. Fix the weak topics, then retry</h2>
<p>Study the topics where you lost the most marks, then attempt similar questions from a different year. Repeat. Progress is easier to see when you test the same skill twice.</p>
<h2>6. Know the layout</h2>
<p>Every subject has a standard paper layout. Look at the cover page and instructions of a past paper so nothing surprises you on the day.</p>
<p>Ready to start? <a href="/">Choose your subject</a>.</p>
"""},
}


# APS calculator page body. The script runs in the browser only; nothing is sent or stored.
# Keep the disclaimer: universities calculate APS differently.
APS_BODY = """
<p>Your Admission Point Score (APS) turns your Grade 12 marks into one number that universities use when they look at applications. Enter your marks below to see a quick estimate.</p>
<div class="calc" id="calc">
<table><thead><tr><th>Subject</th><th>Mark (%)</th><th>Level</th></tr></thead><tbody id="rows"></tbody></table>
<p><label><input type="checkbox" id="lo"> My university counts Life Orientation</label></p>
<p><button type="button" id="go">Calculate my APS</button></p>
<div class="note" id="out" role="status" aria-live="polite">Enter your marks and tap Calculate.</div>
</div>
<h2>How the levels work</h2>
<div class="scroll"><table><thead><tr><th>Mark</th><th>Level</th></tr></thead><tbody>
<tr><td>80 to 100%</td><td>7</td></tr><tr><td>70 to 79%</td><td>6</td></tr><tr><td>60 to 69%</td><td>5</td></tr>
<tr><td>50 to 59%</td><td>4</td></tr><tr><td>40 to 49%</td><td>3</td></tr><tr><td>30 to 39%</td><td>2</td></tr><tr><td>0 to 29%</td><td>1</td></tr>
</tbody></table></div>
<h2>Important</h2>
<p class="note">Every university and every course calculates APS in its own way. Some leave out Life Orientation, some weight Maths or English more, and some use their own scoring. This tool gives a general estimate only. Always check the exact method and required score for your course on the university's own website.</p>
<p>Want to raise your marks? Pick a subject on the <a href="/">home page</a> and practise with past papers.</p>
<script>
(function(){
  var names=["Home language","First additional language","Mathematics or Maths Literacy","Subject 4","Subject 5","Subject 6","Life Orientation"];
  var rows=document.getElementById("rows");
  names.forEach(function(n,i){
    var tr=document.createElement("tr");
    tr.innerHTML='<td>'+n+'</td><td><input type="number" inputmode="numeric" min="0" max="100" id="m'+i+'" aria-label="'+n+' mark" style="width:5.5em;font:inherit;padding:6px"></td><td id="l'+i+'">-</td>';
    rows.appendChild(tr);
  });
  function level(p){return p>=80?7:p>=70?6:p>=60?5:p>=50?4:p>=40?3:p>=30?2:1;}
  function read(i){var v=parseFloat(document.getElementById("m"+i).value);return isNaN(v)?null:Math.max(0,Math.min(100,v));}
  document.getElementById("go").addEventListener("click",function(){
    var lv=[],missing=0;
    for(var i=0;i<7;i++){
      var v=read(i),cell=document.getElementById("l"+i);
      if(v===null){cell.textContent="-";if(i<6)missing++;lv.push(null);}
      else{var l=level(v);cell.textContent=l;lv.push(l);}
    }
    var out=document.getElementById("out");
    if(missing>0){out.textContent="Enter marks for the first 6 subjects to calculate.";return;}
    var six=lv.slice(0,6).reduce(function(a,b){return a+b;},0);
    var msg="Six subjects (without Life Orientation): "+six+" out of 42.";
    if(document.getElementById("lo").checked){
      if(lv[6]===null){msg+=" Add your Life Orientation mark to include it.";}
      else{msg+=" Including Life Orientation: "+(six+lv[6])+" out of 49.";}
    }
    out.textContent=msg;
  });
})();
</script>
"""

# Site styles (one small file). Colours are variables at the top so you can re-theme in one place.
CSS = """
:root{--ink:#14212B;--paper:#F6F8F7;--teal:#0E6B5C;--teal-d:#0A4E43;--mark:#F2D35E;--line:#C5D0CC;--muted:#4A5A64;--card:#FFFFFF}
@media (prefers-color-scheme:dark){:root{--ink:#E8EEEC;--paper:#0F1A1F;--teal:#5CC4AE;--teal-d:#7FD6C3;--mark:#7A6A22;--line:#2B3A41;--muted:#9FB1B8;--card:#16252C}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--teal);text-underline-offset:3px}
a:focus-visible,button:focus-visible{outline:3px solid var(--mark);outline-offset:2px}
.wrap{max-width:720px;margin:0 auto;padding:0 18px}
header.site{border-bottom:1px solid var(--line);background:var(--card)}
header.site .wrap{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding-top:12px;padding-bottom:12px}
.brand{font:700 1.25rem Georgia,"Times New Roman",serif;color:var(--ink);text-decoration:none}
nav a{margin-left:14px;font-size:.95rem;text-decoration:none}
h1,h2,h3{font-family:Georgia,"Times New Roman",serif;line-height:1.25;margin:1.6em 0 .5em}
h1{font-size:2rem;margin-top:1em}
h2{font-size:1.4rem}
.cover{border:2px solid var(--ink);padding:5px;margin:22px 0 26px;background:var(--card)}
.cover-in{border:1px solid var(--ink);padding:18px 18px 14px}
.cover h1{margin:0 0 .4em;font-size:1.9rem}
.cover p{margin:.3em 0}
.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:14px 0 0;padding-top:12px;border-top:1px solid var(--ink);font-size:.95rem}
.facts dt{font-weight:700}.facts dd{margin:0}
mark{background:var(--mark);color:inherit;padding:0 .2em}
.grid{display:grid;grid-template-columns:1fr;gap:10px;padding:0;list-style:none;margin:14px 0}
.grid a{display:block;padding:13px 16px;background:var(--card);border:1px solid var(--line);border-left:6px solid var(--teal);text-decoration:none;color:var(--ink);font-weight:600}
.grid a:hover{border-left-color:var(--mark)}
@media(min-width:560px){.grid{grid-template-columns:1fr 1fr}}
.calc input[type=number]{border:1px solid var(--line);background:var(--card);color:var(--ink)}
button{font:inherit;font-weight:700;padding:10px 18px;background:var(--teal);color:#fff;border:0;cursor:pointer}
@media (prefers-color-scheme:dark){button{color:#0F1A1F}}
table{width:100%;border-collapse:collapse;font-size:.95rem;margin:12px 0}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}
.scroll{overflow-x:auto}
.note{border-left:6px solid var(--mark);background:var(--card);padding:12px 16px;margin:20px 0;font-size:.95rem}
.crumbs{font-size:.9rem;margin-top:16px;color:var(--muted)}
footer{border-top:1px solid var(--line);margin-top:48px;padding:22px 0 36px;font-size:.9rem;color:var(--muted)}
footer a{margin-right:14px}
ul.tick{padding-left:1.2em}ul.tick li{margin:.4em 0}
"""

# Safely escape text for use inside HTML. Use it on ALL data-file values.
def esc(s): return html.escape(s, quote=True)

# Returns the top of every page: <head> tags (SEO, canonical, AdSense script) and the header/nav.
def head(title, desc, path):
    canon = f"{DOMAIN}{path}"
    ads = (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={ADSENSE_CLIENT}" crossorigin="anonymous"></script>'
           if ADSENSE_CLIENT else "")
    return f"""<!doctype html>
<html lang="en-ZA"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{canon}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{canon}">
<meta name="theme-color" content="#0E6B5C">
<link rel="stylesheet" href="/style.css">
{ads}
</head><body>
<header class="site"><div class="wrap"><a class="brand" href="/">{esc(SITE_NAME)}</a>
<nav aria-label="Main"><a href="/">Subjects</a><a href="/aps-calculator.html">APS calculator</a><a href="/how-to-use-past-papers.html">Study method</a><a href="/about.html">About</a></nav></div></header>
<main class="wrap">
"""

# Returns the footer shown on every page (includes the not-affiliated disclaimer; keep it).
def foot():
    return f"""
</main>
<footer><div class="wrap">
<p><a href="/about.html">About</a><a href="/privacy.html">Privacy</a><a href="/contact.html">Contact</a></p>
<p>{esc(SITE_NAME)} is an independent study site and is not affiliated with the Department of Basic Education. Past papers belong to their respective owners. Always confirm details on the official source.</p>
</div></footer></body></html>"""

# Writes one output file into ./dist, creating folders as needed.
def write(path, content):
    full = os.path.join("dist", path.lstrip("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f: f.write(content)

def load_papers():
    """Merge scraped links (papers_auto.json) with your manual ones (papers.json)."""
    merged = {}
    for fname in ("papers_auto.json", "papers.json"):
        try:
            with open(fname, encoding="utf-8") as f: data = json.load(f)
        except FileNotFoundError:
            continue
        for k, v in data.items():
            if isinstance(v, list): merged.setdefault(k, []).extend(v)
    return merged

# Builds JSON-LD breadcrumb data so Google can show the page path in results.
def breadcrumb_ld(items):
    return json.dumps({"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[
        {"@type":"ListItem","position":i+1,"name":n,"item":DOMAIN+u} for i,(n,u) in enumerate(items)]})

# Builds one subject page: intro text, table of links from the data files, focus tips and a study plan.
# Edit the SUBJECTS list near the top to change wording; add a subject by adding one dictionary there.
def subject_page(s, papers):
    path = f"/{s['slug']}.html"
    title = f"Grade 12 {s['name']} Past Papers and Memos (NSC) | {SITE_NAME}"
    desc = f"Find official Grade 12 {s['name']} past papers and memos, see what each paper covers, and get a study plan built around past-paper practice."
    rows = ""
    for p in sorted(papers.get(s["slug"], []), key=lambda x: (-int(x.get("year",0)), x.get("sitting",""), x.get("label",""))):
        qp = f'<a href="{esc(p["qp"])}" rel="noopener">Paper</a>' if p.get("qp") else "-"
        mm = f'<a href="{esc(p["memo"])}" rel="noopener">Memo</a>' if p.get("memo") and p.get("memo") != p.get("qp") else "-"
        ex = ", ".join(f'<a href="{esc(e["url"])}" rel="noopener">{esc(e["name"])}</a>' for e in p.get("extras", [])) or "-"
        if not (p.get("qp") or p.get("memo") or p.get("extras")): continue
        rows += f"<tr><td>{esc(str(p.get('year','')))}</td><td>{esc(p.get('sitting',''))}</td><td>{esc(p.get('label',''))}</td><td>{qp}</td><td>{mm}</td><td>{ex}</td></tr>"
    table = (f'<div class="scroll"><table><thead><tr><th>Year</th><th>Sitting</th><th>Paper</th><th>Download</th><th>Memo</th><th>Extras</th></tr></thead><tbody>{rows}</tbody></table></div>'
             if rows else
             f'<p>Direct links for {esc(s["name"])} are being added. Until then, use the <a href="{DBE_PAGE}" rel="noopener">official Department of Basic Education past papers page</a>, choose the year, then open the {esc(s["name"])} entry.</p>')
    paper_list = "".join(f"<li>{esc(x)}</li>" for x in s["papers"])
    focus = "".join(f"<li>{esc(x)}</li>" for x in s["focus"])
    others = "".join(f'<li><a href="/{o["slug"]}.html">{esc(o["name"])}</a></li>' for o in SUBJECTS if o["slug"] != s["slug"])
    body = head(title, desc, path)
    body += f'<script type="application/ld+json">{breadcrumb_ld([("Home","/"),(s["name"],path)])}</script>'
    body += f"""<p class="crumbs"><a href="/">Home</a> / {esc(s['name'])}</p>
<h1>Grade 12 {esc(s['name'])} past papers and memos</h1>
<p>Practising with real papers is one of the most reliable ways to prepare for {esc(s['name'])}. Below you will find what the papers cover, where to get them, and how to turn each one into progress.</p>
<h2>What the papers cover</h2>
<ul class="tick">{paper_list}</ul>
<p class="note">Time allowed and total marks are printed on each paper's cover page, and can change between years. Check the cover page of the paper you are writing.</p>
<h2>Download {esc(s['name'])} papers</h2>
{table}
<h2>What to focus on</h2>
<ul class="tick">{focus}</ul>
<h2>A simple plan</h2>
<p>{esc(s['tip'])}</p>
<p>New to this? Read <a href="/how-to-use-past-papers.html">how to use past papers</a> for the full method.</p>
<h2>Other subjects</h2>
<ul class="grid">{others}</ul>
"""
    body += foot()
    write(path, body)
    return path

# Main entry point: wipes ./dist, then writes every page, the sitemap, robots.txt and ads.txt.
def build():
    if os.path.exists("dist"): shutil.rmtree("dist")
    os.makedirs("dist")
    write("/style.css", CSS)
    papers = load_papers()
    urls = ["/"]
    subj_links = "".join(f'<li><a href="/{s["slug"]}.html">{esc(s["name"])}</a></li>' for s in SUBJECTS)

    # home
    title = f"Grade 12 Past Papers and Memos: Subject Guides | {SITE_NAME}"
    desc = "Find Grade 12 NSC past papers and memos by subject, see what each paper covers, and learn a study method that works."
    h = head(title, desc, "/")
    h += f"""<div class="cover"><div class="cover-in">
<h1>Grade 12 past papers, sorted by subject</h1>
<p>Pick a subject, see what its papers cover, get the official download, and follow a plan for using it.</p>
<dl class="facts"><dt>Level</dt><dd>National Senior Certificate, Grade 12</dd><dt>Source</dt><dd>Department of Basic Education</dd><dt>Updated</dt><dd>{TODAY}</dd></dl>
</div></div>
<h2>Choose your subject</h2>
<ul class="grid">{subj_links}</ul>
<p class="note"><mark>Applying to university?</mark> Work out your score with the <a href="/aps-calculator.html">APS calculator</a>.</p>
<p class="note"><mark>Start here:</mark> not sure how to study with past papers? Read the <a href="/how-to-use-past-papers.html">step-by-step method</a>.</p>
<h2>Where the papers come from</h2>
<p>Official National Senior Certificate papers are published free by the Department of Basic Education. We organise them by subject and explain how to use them. Browse the <a href="{DBE_PAGE}" rel="noopener">official past papers page</a> any time.</p>
"""
    h += foot()
    write("/index.html", h)

    for s in SUBJECTS:
        urls.append(subject_page(s, papers))

    for slug, p in PAGES_EXTRA.items():
        path = f"/{slug}.html"
        b = head(f"{p['title']} | {SITE_NAME}", p["desc"], path)
        b += f"<h1>{esc(p['title'])}</h1>" + p["body"] + foot()
        write(path, b); urls.append(path)

    ap = head(f"APS Calculator for South African Universities | {SITE_NAME}", "Quick estimate of your Admission Point Score (APS) from your Grade 12 marks. Free calculator with level table.", "/aps-calculator.html")
    ap += "<h1>APS calculator</h1>" + APS_BODY + foot()
    write("/aps-calculator.html", ap); urls.append("/aps-calculator.html")

    about = head(f"About | {SITE_NAME}", f"About {SITE_NAME}.", "/about.html") + f"""
<h1>About {esc(SITE_NAME)}</h1>
<p>{esc(SITE_NAME)} helps Grade 12 learners find past papers and study with them effectively. We organise subjects, explain what each paper covers and share study methods.</p>
<p>We are not affiliated with the Department of Basic Education. Papers are published by their owners; we link to official sources where possible. Spotted a broken link or an error? <a href="/contact.html">Tell us</a>.</p>""" + foot()
    contact = head(f"Contact | {SITE_NAME}", "Contact us.", "/contact.html") + f"""
<h1>Contact</h1><p>Email us at <a href="mailto:{esc(CONTACT_EMAIL)}">{esc(CONTACT_EMAIL)}</a> with corrections, broken links or suggestions.</p>""" + foot()
    privacy = head(f"Privacy Policy | {SITE_NAME}", "How we handle your data.", "/privacy.html") + f"""
<h1>Privacy policy</h1>
<p>Last updated {TODAY}.</p>
<p>{esc(SITE_NAME)} does not ask visitors to create an account or submit personal information. We may use cookies and similar technologies for site analytics and to show advertising.</p>
<h2>Advertising</h2>
<p>Third-party vendors, including Google, use cookies to serve ads based on your visits to this and other websites. You can opt out of personalised advertising in Google's Ads Settings.</p>
<h2>Analytics</h2>
<p>We use privacy-conscious analytics to understand which pages are used so we can improve them.</p>
<h2>Your rights</h2>
<p>If you have questions about your information, contact <a href="mailto:{esc(CONTACT_EMAIL)}">{esc(CONTACT_EMAIL)}</a>. We aim to handle personal information in line with South Africa's POPIA.</p>""" + foot()
    for path, c in (("/about.html",about),("/contact.html",contact),("/privacy.html",privacy)):
        write(path, c); urls.append(path)

    nf = head(f"Page not found | {SITE_NAME}", "Page not found.", "/404.html") + "<h1>That page does not exist</h1><p>Try the <a href='/'>subject list</a>.</p>" + foot()
    write("/404.html", nf)

    sm = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sm += "".join(f"<url><loc>{DOMAIN}{u}</loc><lastmod>{TODAY}</lastmod></url>\n" for u in urls) + "</urlset>\n"
    write("/sitemap.xml", sm)
    write("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {DOMAIN}/sitemap.xml\n")
    if ADSENSE_CLIENT:
        write("/ads.txt", f"google.com, {ADSENSE_CLIENT.replace('ca-','')}, DIRECT, f08c47fec0942fa0\n")
    print(f"Built {len(urls)} pages into ./dist")



# ############################################################################
# SECTION 3 OF 3 : COMMAND LINE
# ############################################################################
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    rest = sys.argv[2:]
    if cmd == "scrape":
        sys.argv = [sys.argv[0]] + rest
        scrape_main()
    elif cmd == "build":
        build()
    elif cmd == "all":
        sys.argv = [sys.argv[0]] + rest
        scrape_main()
        build()
    else:
        sys.exit(__doc__)
