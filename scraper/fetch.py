"""
Tarrant County (Fort Worth / DFW) Motivated Seller Lead Scraper v1.0
Source: tarrant.tx.publicsearch.us — FC department (Foreclosure Notices)

Same PublicSearch.us vendor platform as bexar-leads/nueces-leads/dallas-leads,
confirmed live 2026-10-01 -- but NOT the same table layout. Verified live
before writing this: Tarrant's FC listing has only 4 real columns (col-3
Grantor, col-4 Sale Date [month/year only], col-5 Filed Date, col-6 Property
Address) and, critically, NO document number and NO link/href anywhere in
the row -- confirmed via direct DOM inspection (`links: []`). Bexar's column
layout (doc type/recorded date/sale date/DOC NUMBER/remarks/address) does
not apply here; don't assume one county's column shape from another's, that
cost real time tonight already.

Dedup key: since there's no real doc number to key on, doc_number is a
stable synthetic id (filed date + a short hash of grantor+address) rather
than a row-index-based id -- row order for the same filed_date can shift
slightly run to run as more same-day records get indexed, so an index-based
key would silently create duplicate "new" leads on every re-run.

Mechanism (proven on bexar-leads tonight after a multi-hour live
investigation, see that repo's 2026-10-01 commits):
  - searchType=quickSearch + instrumentDateRange spanning the site's full
    index (NOT a narrowed recent window -- narrowing the date range is dead
    on this platform family; relies on early-stop against known_docs
    instead of the site narrowing anything for us).
  - about:blank reset before every navigation (React SPA client-side-route
    transitions can render an empty/decoy state before real data loads).
  - "confirmed empty" requires BOTH "No Results Found" AND "Suggestions:"
    text present, not a bare "No Results" heading alone.

v1.0 scope: FC (Foreclosure Notice) discovery only -- grantor (used
directly as owner, since this column already gives it, unlike Bexar's
listing), sale date, filed date, raw property address. No loan/lender/
owner-mailing-address enrichment yet (needs Tarrant Appraisal District's
own ArcGIS endpoint, not yet researched -- a flagged follow-up, not
guessed at here).
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PUBLICSEARCH_BASE = "https://tarrant.tx.publicsearch.us"
KEEP_DAYS    = 90   # rolling lookback window, filtered client-side
MAX_PAGES    = 20   # absolute cap per run -- first run has no known_docs yet,
                     # so early-stop can't kick in until genuinely old rows appear
PAGE_TIMEOUT = 40

RECORDS_PATH = Path("dashboard/records.json")
TODAY = datetime.now(timezone.utc)
CUTOFF_DATE = TODAY - timedelta(days=KEEP_DAYS)


def get_driver():
    # Matches bexar-leads' proven get_driver() exactly (2026-10-01) --
    # ChromeDriverManager with a bare-webdriver.Chrome fallback, same UA/
    # flags. That file's own history shows headless/plain-Chrome variants
    # were tested and ruled out as the cause of that county's issues, so
    # there's no reason to deviate here on a brand-new county.
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-web-security")
    opts.add_argument("--allow-running-insecure-content")

    try:
        from selenium.webdriver.chrome.service import Service as ChromeService
        from webdriver_manager.chrome import ChromeDriverManager
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
    except Exception:
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(PAGE_TIMEOUT)
    return driver


def _confirmed_empty(driver):
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return False
    return "No Results Found" in body_text and "Suggestions:" in body_text


def parse_full_date(raw):
    """MM/DD/YYYY -> datetime, or None."""
    raw = (raw or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def parse_month_year(raw):
    """'Dec 2026' -> datetime (1st of month), or None."""
    raw = (raw or "").strip()
    m = re.match(r"^([A-Za-z]{3})\w*\s+(\d{4})$", raw)
    if not m or m.group(1)[:3] not in MONTH_MAP:
        return None
    try:
        return datetime(int(m.group(2)), MONTH_MAP[m.group(1)[:3]], 1, tzinfo=timezone.utc)
    except ValueError:
        return None


def looks_like_address(raw):
    raw = (raw or "").strip()
    if not raw or raw.upper() == "N/A":
        return False
    return bool(re.match(r"^\d+\s+[A-Z]", raw.upper()))


def clean_address(raw):
    raw = re.sub(r"\s+", " ", raw or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    street = parts[0].upper() if parts else ""
    city = parts[1].upper() if len(parts) > 1 else ""
    zip_m = re.search(r"\b(\d{5})\b", raw)
    zip_code = zip_m.group(1) if zip_m else ""
    return street, city, zip_code


def make_doc_number(filed_date_raw, grantor, address_raw):
    """Stable synthetic id -- see module docstring for why this isn't a
    simple row-index (same-day row order isn't stable across runs)."""
    d = parse_full_date(filed_date_raw)
    date_part = d.strftime("%Y%m%d") if d else "00000000"
    h = hashlib.md5(f"{grantor}|{address_raw}".encode("utf-8")).hexdigest()[:8]
    return f"TAR-{date_part}-{h}"


def scrape_fc(driver, known_docs):
    """Foreclosure Notice discovery -- see module docstring for the mechanism."""
    far_future = (TODAY + timedelta(days=180)).strftime("%Y%m%d")
    url_base = (
        f"{PUBLICSEARCH_BASE}/results"
        f"?department=FC"
        f"&instrumentDateRange=20000404%2C{far_future}"
        f"&keywordSearch=false"
        f"&limit=50"
        f"&sort=desc"
        f"&sortBy=recordedDate"
        f"&searchType=quickSearch"
    )

    records = []
    offset = 0
    page = 0
    zero_new_streak = 0
    prev_page_was_full = False

    while True:
        page_num = page + 1
        if page_num > MAX_PAGES:
            log.warning(f"  Hit MAX_PAGES={MAX_PAGES} — stopping, rest deferred to next run")
            break

        url = f"{url_base}&offset={offset}"
        log.info(f"  offset={offset}")

        try:
            driver.get("about:blank")
        except Exception:
            pass
        try:
            driver.get(url)
        except Exception as e:
            log.warning(f"  Page load failed offset={offset}: {e}")
            break

        deadline = time.time() + PAGE_TIMEOUT
        rows = []
        while time.time() < deadline:
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if rows:
                break
            if _confirmed_empty(driver):
                break
            time.sleep(0.4)

        if not rows:
            if prev_page_was_full:
                log.warning(
                    f"  SUSPICIOUS STOP: page {page} was a full 50-row page, but "
                    f"page {page+1} found nothing — verify manually before trusting this run."
                )
            log.info("  No results — stopping")
            break

        prev_page_was_full = len(rows) >= 48
        page_new = 0
        page_known = 0

        for row in rows:
            try:
                cols = driver.execute_script(
                    """
                    const row = arguments[0];
                    const tds = row.querySelectorAll('td');
                    return Array.from(tds).map(td => td.innerText);
                    """,
                    row,
                )
            except Exception:
                continue
            if len(cols) < 7:
                continue

            grantor = (cols[3] or "").strip()
            sale_date_raw = (cols[4] or "").strip()
            filed_date_raw = (cols[5] or "").strip()
            address_raw = (cols[6] or "").strip()

            if grantor.upper() == "N/A":
                grantor = ""

            filed_dt = parse_full_date(filed_date_raw)
            if filed_dt and filed_dt < CUTOFF_DATE:
                continue

            doc_number = make_doc_number(filed_date_raw, grantor, address_raw)
            if doc_number in known_docs:
                page_known += 1
                continue

            has_address = looks_like_address(address_raw)
            street, city, zip_code = clean_address(address_raw) if has_address else ("", "", "")

            sale_dt = parse_month_year(sale_date_raw)
            days_until_sale = (sale_dt.date() - TODAY.date()).days if sale_dt else None

            flags = ["NEW"]
            if not has_address:
                flags.append("NO ADDRESS - LEGAL DESC ONLY")
            if not grantor:
                flags.append("NO OWNER - PARSE MISS")
            if days_until_sale is not None:
                if days_until_sale <= 14:
                    flags.append("URGENT")
                elif days_until_sale <= 30:
                    flags.append("AUCTION SOON")

            rec = {
                "type": "NOF",
                "source": "publicsearch",
                "county": "tarrant",
                "owner": grantor.title() if grantor else "",
                "address": street,
                "city": city,
                "zip": zip_code,
                "mail_addr": "",
                "absentee": False,
                "duplicate": False,
                "is_new": True,
                "doc_number": doc_number,
                "date_filed": filed_date_raw,
                "date_recorded": filed_date_raw,
                "sale_date": sale_date_raw,
                "days_until_sale": days_until_sale,
                "run_ts": TODAY.isoformat(),
                "flags": flags,
                "lender": "",
                "loan_amount": "",
                "loan_date": "",
                "trustee": "",
                "tenure_years": None,
                "tenure_score_bonus": 0,
                "prop_id": "",
                "deed_date": "",
                "last_sale_amt": "",
                "appraised_value": "",
                "appr_history": [],
                "appr_trend": "",
                "stacked": False,
                "score": (8 if days_until_sale is not None and days_until_sale <= 30 else 6) if has_address else 4,
            }
            records.append(rec)
            known_docs.add(doc_number)
            page_new += 1

        log.info(f"  Page {page+1}: {page_new} new | {page_known} known ({len(rows)} rows)")

        if page_new == 0:
            zero_new_streak += 1
        else:
            zero_new_streak = 0
        if zero_new_streak >= 2 and page > 1:
            log.info("  2 consecutive pages with 0 new — stopping early")
            break

        offset += 50
        page += 1
        time.sleep(1.5)

    return records


def build_dashboard(records):
    os.makedirs("dashboard", exist_ok=True)
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
    with open(RECORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, separators=(",", ":"), ensure_ascii=True)
    log.info(f"Dashboard: {len(clean)} records, {RECORDS_PATH.stat().st_size:,} bytes")


def main():
    log.info("=" * 60)
    log.info("Tarrant County Lead Scraper v1.0 (FC discovery only)")
    log.info(f"Run: {TODAY.isoformat()}")
    log.info("=" * 60)

    existing = []
    if RECORDS_PATH.exists():
        try:
            existing = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
            log.info(f"Loaded {len(existing)} existing records")
        except Exception as e:
            log.warning(f"Could not load existing records: {e}")

    for r in existing:
        r["is_new"] = False

    known_docs = {r["doc_number"] for r in existing if r.get("doc_number")}

    driver = get_driver()
    try:
        new_records = scrape_fc(driver, known_docs)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    log.info(f"FC: {len(new_records)} new records")
    all_records = existing + new_records
    build_dashboard(all_records)
    log.info("Done.")


if __name__ == "__main__":
    main()
