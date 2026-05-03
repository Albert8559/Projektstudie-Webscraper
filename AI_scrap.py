import pandas as pd
import re
import asyncio
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
INPUT_FILE = "urls.csv"
OUTPUT_FILE = "results_enriched.csv"

MAX_CONCURRENT_SCRAPES = 5
MAX_CONCURRENT_ENRICH = 5
RETRIES = 2


# =========================
# REGEX
# =========================
PATENT_REGEX = re.compile(
    r'\b(?:US|EP|WO|CN|JP|DE|GB|FR)\s?\d{4,}(?:[A-Z]\d?)?\b'
)

def generate_google_patent_pages(query="siemens", pages=20):
    base = "https://patents.google.com/"
    return [
        f"{base}?q=({query})&num=100&page={i}"
        for i in range(1, pages + 1)
    ]
# =========================
# HELPERS
# =========================

def normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def is_blocked(text: str) -> bool:
    signals = ["captcha", "verify you are human", "access denied"]
    text = text.lower()
    return any(s in text for s in signals)


def extract_patents(text):
    matches = PATENT_REGEX.findall(text)
    return list(set(m.strip().replace(" ", "") for m in matches))


def compute_expiry(filing_date):
    try:
        dt = datetime.strptime(filing_date, "%Y-%m-%d")
        return dt.replace(year=dt.year + 20).strftime("%Y-%m-%d")
    except:
        return None


# =========================
# RETRY WRAPPER
# =========================

async def with_retries(coro, *args, **kwargs):
    for attempt in range(RETRIES + 1):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            if attempt == RETRIES:
                print(f"❌ Failed after retries: {e}")
                return None
            await asyncio.sleep(2 ** attempt)


# =========================
# SEMAPHORES
# =========================

scrape_sem = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)
enrich_sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICH)


# =========================
# SCRAPING
# =========================

async def scrape_worker(context, url):
    async with scrape_sem:
        page = await context.new_page()
        patents = set()

        try:
            url = normalize_url(url)
            print(f"🔎 {url}")

            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("networkidle")

            content = await page.content()

            if not is_blocked(content):
                patents.update(extract_patents(content))

        except Exception as e:
            print(f"⚠️ Scrape error {url}: {e}")

        await page.close()
        return patents


# =========================
# EXPIRATION EXTRACTION
# =========================

async def extract_expiration(page):
    try:
        elements = page.locator('div.legal-status')
        count = await elements.count()

        dates = []

        for i in range(count):
            text = await elements.nth(i).text_content()
            if text:
                text = text.strip()
                if re.match(r"\d{4}-\d{2}-\d{2}", text):
                    dates.append(text)

        if not dates:
            return None

        # Use latest date (usually adjusted expiration)
        return sorted(dates)[-1]

    except:
        return None


# =========================
# ENRICHMENT
# =========================

async def enrich_patent_playwright(context, patent_number):
    page = await context.new_page()

    data = {
        "patent": patent_number,
        "jurisdiction": patent_number[:2],
        "assignee": None,
        "status": None,
        "filing_date": None,
        "expiration_date": None,
        "abstract": None,
    }

    try:
        url = f"https://patents.google.com/patent/{patent_number}"
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle")

        # -------------------------
        # ASSIGNEE (CURRENT FIRST)
        # -------------------------
        try:
            val = await page.locator('dd[itemprop="assigneeCurrent"]').first.text_content()
            if val:
                data["assignee"] = val.strip()
        except:
            pass

        # fallback
        if not data["assignee"]:
            try:
                val = await page.locator('dd[itemprop="assigneeOriginal"]').first.text_content()
                if val:
                    data["assignee"] = val.strip()
            except:
                pass

        # -------------------------
        # STATUS
        # -------------------------
        try:
            val = await page.locator('span[itemprop="legalStatus"]').first.text_content()
            if val:
                data["status"] = val.strip()
        except:
            pass

        # -------------------------
        # FILING DATE
        # -------------------------
        try:
            val = await page.locator('time[itemprop="filingDate"]').first.text_content()
            if val:
                data["filing_date"] = val.strip()
        except:
            pass

        # -------------------------
        # ABSTRACT
        # -------------------------
        try:
            val = await page.locator('div.abstract').inner_text()
            if val:
                data["abstract"] = val.strip()
        except:
            pass

        # -------------------------
        # EXPIRATION (REAL FIRST)
        # -------------------------
        expiry = await extract_expiration(page)

        if expiry:
            data["expiration_date"] = expiry
        elif data["filing_date"]:
            data["expiration_date"] = compute_expiry(data["filing_date"])

    except Exception as e:
        print(f"⚠️ Enrich error {patent_number}: {e}")

    await page.close()
    return data


async def enrich_worker(context, patent):
    async with enrich_sem:
        return await with_retries(enrich_patent_playwright, context, patent)


# =========================
# MAIN
# =========================

async def main():
    # -------------------------
    # CONFIGURE QUERY + PAGES
    # -------------------------
    QUERY = "siemens"
    NUM_PAGES = 5

    def generate_google_patent_pages(query, pages):
        base = "https://patents.google.com/"
        return [
            f"{base}?q=({query})&num=100&page={i}"
            for i in range(1, pages + 1)
        ]

    urls = generate_google_patent_pages(QUERY, NUM_PAGES)

    print(f"🚀 Processing {len(urls)} Google Patent pages for query: '{QUERY}'\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )

        # -------------------------
        # STEP 1: SCRAPE SEARCH PAGES
        # -------------------------
        scrape_tasks = [scrape_worker(context, url) for url in urls]
        scrape_results = await asyncio.gather(*scrape_tasks)

        # Flatten + deduplicate
        all_patents = set().union(*scrape_results)

        print(f"\n🧾 Unique patents found: {len(all_patents)}\n")
        print(all_patents)

        # -------------------------
        # STEP 2: ENRICH PATENTS
        # -------------------------
        enrich_tasks = [
            enrich_worker(context, patent)
            for patent in all_patents
        ]

        enriched_results = await asyncio.gather(*enrich_tasks)
        enriched_results = [r for r in enriched_results if r]

        await browser.close()

    # -------------------------
    # SAVE OUTPUT
    # -------------------------
    df_out = pd.DataFrame(enriched_results)
    df_out.to_csv(OUTPUT_FILE, index=False)

    print(f"\n✅ Done → {OUTPUT_FILE}")


# =========================
# RUN
# =========================

if __name__ == "__main__":
    asyncio.run(main())