import pandas as pd
import asyncio
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "courtlistener_enriched_3.csv"

# Concurrency settings
MAX_CONCURRENT_SCRAPES = 3
MAX_CONCURRENT_ENRICH = 3
RETRIES = 3

# Search Parameters
SEARCH_QUERY = "%22Patent+Marking%22"
BASE_URL = "https://www.courtlistener.com"
NUM_PAGES = 11


# =========================
# ANALYSIS LOGIC (HEURISTICS)
# =========================

def analyze_legal_content(text: str):
    """
    Improved legal outcome + damages extraction.
    Focuses on tail sections where judgments/orders usually appear.
    """

    # -----------------------------------
    # FOCUS ON END OF DOCUMENT
    # -----------------------------------
    # Most legal outcomes are near the end
    text = text[-20000:]

    text_lower = text.lower()

    result = {
        "outcome": "unknown",
        "payment_found": 0,
        "payment_amount": None
    }

    # -----------------------------------
    # OUTCOME DETECTION
    # -----------------------------------

    plaintiff_signals = [
        "judgment for plaintiff",
        "plaintiff prevailed",
        "plaintiff wins",
        "defendant is liable",
        "in favor of plaintiff",
        "motion granted for plaintiff",
        "permanent injunction granted"
    ]

    defendant_signals = [
        "judgment for defendant",
        "defendant prevailed",
        "plaintiff takes nothing",
        "case dismissed",
        "dismissed with prejudice",
        "summary judgment for defendant",
        "motion granted for defendant"
    ]

    mixed_signals = [
        "granted in part and denied in part",
        "affirmed in part",
        "reversed in part",
        "mixed verdict"
    ]

    settlement_signals = [
        "settlement",
        "settled",
        "stipulated dismissal"
    ]

    # Scores
    plaintiff_score = sum(1 for s in plaintiff_signals if s in text_lower)
    defendant_score = sum(1 for s in defendant_signals if s in text_lower)
    mixed_score = sum(1 for s in mixed_signals if s in text_lower)

    # Determine outcome
    if any(s in text_lower for s in settlement_signals):
        result["outcome"] = "settled"

    elif mixed_score > 0:
        result["outcome"] = "mixed"

    elif plaintiff_score > defendant_score and plaintiff_score > 0:
        result["outcome"] = "plaintiff_win"

    elif defendant_score > plaintiff_score and defendant_score > 0:
        result["outcome"] = "defendant_win"

    elif "dismissed" in text_lower:
        result["outcome"] = "dismissed"

    # -----------------------------------
    # PAYMENT EXTRACTION
    # -----------------------------------

    payment_signals = [
        "damages",
        "awarded",
        "award",
        "settlement",
        "liable for",
        "ordered to pay",
        "attorney fees",
        "reasonable royalty",
        "enhanced damages"
    ]

    # Better money regex
    money_pattern = re.compile(
        r'(?:\$|USD\s?)\s?([\d,.]+(?:\.\d+)?)\s*(million|billion|thousand)?',
        re.IGNORECASE
    )

    sentences = re.split(r'(?<=[.!?])\s+', text)

    for sentence in sentences:

        s_lower = sentence.lower()

        # Require BOTH payment language + money
        if any(signal in s_lower for signal in payment_signals):

            match = money_pattern.search(sentence)

            if match:

                amount = match.group(0)

                result["payment_found"] = 1
                result["payment_amount"] = amount.strip()

                break

    return result

# =========================
# HELPERS
# =========================

def generate_courtlistener_urls(query, pages):
    url_template = (
        f"{BASE_URL}/?q={query}"
        "&type=o&order_by=dateFiled+desc&stat_Published=on&page={page_num}"
    )
    return [url_template.format(page_num=i) for i in range(1, pages + 1)]


async def with_retries(coro, *args, **kwargs):
    for attempt in range(RETRIES + 1):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            if attempt == RETRIES:
                print(f"❌ Failed after {RETRIES} retries: {e}")
                return None
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️ Attempt {attempt + 1} failed. Retrying in {wait_time:.2f}s...")
            await asyncio.sleep(wait_time)


scrape_sem = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)
enrich_sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICH)


# =========================
# PHASE 1: SCRAPE SEARCH PAGES
# =========================

async def scrape_search_page(context, url):
    async with scrape_sem:
        page = await context.new_page()
        results_on_page = []

        try:
            print(f"🔎 [Search] Scraping: {url}")
            await page.goto(url, timeout=60000)
            await page.wait_for_selector("article", timeout=30000)
            
            articles = await page.locator("article").all()

            for article in articles:
                data = {}
                try:
                    # Name & URL
                    title_elem = article.locator('h3.bottom.serif a.visitable')
                    if await title_elem.count() > 0:
                        data["case_name"] = await title_elem.inner_text()
                        href = await title_elem.get_attribute("href")
                        data["url"] = BASE_URL + href if href else None
                    else:
                        continue 

                    # Date
                    time_elem = article.locator('div.bottom div.inline-block time').first
                    data["date_filed"] = await time_elem.get_attribute("datetime") if await time_elem.count() > 0 else None

                    # Docket
                    docket_elem = article.locator('span.meta-data-value.select-all').first
                    data["docket_number"] = await docket_elem.inner_text() if await docket_elem.count() > 0 else None

                    if data.get("case_name"):
                        results_on_page.append(data)

                except Exception:
                    continue

        except Exception as e:
            print(f"⚠️ [Search] Error on {url}: {e}")
        
        finally:
            await page.close()
            
        return results_on_page

async def extract_court(page, url):
    """
    Robust court extraction across CourtListener layouts.
    """

    selectors = [
        "h4.case-court",
        "p.court",
        ".court-name",
        "ol.breadcrumb li",
        "nav.breadcrumbs",
        "dt:has-text('Court') + dd"
    ]

    for selector in selectors:

        try:
            locator = page.locator(selector).first

            if await locator.count() > 0:

                text = await locator.inner_text()

                if text and len(text.strip()) > 3:
                    return text.strip()

        except:
            continue

    # -----------------------------------
    # URL FALLBACKS
    # -----------------------------------

    url_lower = url.lower()

    if "/ca" in url_lower:
        return "US Court of Appeals"

    if "/district-courts/" in url_lower:
        return "US District Court"

    if "/bankruptcy/" in url_lower:
        return "US Bankruptcy Court"

    return None
# =========================
# PHASE 2: ENRICH CASE DETAILS
# =========================

async def enrich_case_details(context, case_data):

    async with enrich_sem:

        if not case_data.get("url"):
            return case_data

        page = await context.new_page()

        try:
            url = case_data["url"]

            print(f"🧐 [Enrich] Processing: {case_data['case_name'][:50]}...")

            await page.goto(url, timeout=60000)

            # -----------------------------------
            # ROBUST WAIT
            # -----------------------------------
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except:
                pass

            # -----------------------------------
            # 1. EXTRACT COURT
            # -----------------------------------
            case_data["court"] = await extract_court(page, url)

            # -----------------------------------
            # 2. EXTRACT TEXT
            # -----------------------------------
            full_text = ""

            selectors = [
                "div#opinion-content",
                "div.col-sm-9.main.document",
                "div.opinion-text",
                "article",
                "main",
                "body"
            ]

            for selector in selectors:

                try:
                    locator = page.locator(selector).first

                    if await locator.count() > 0:

                        text = await locator.inner_text()

                        # Ignore tiny blocks
                        if text and len(text) > 1000:
                            full_text = text
                            break

                except:
                    continue

            # -----------------------------------
            # 3. ANALYZE CONTENT
            # -----------------------------------
            if not full_text:

                case_data["outcome"] = "unknown"
                case_data["payment_found"] = 0
                case_data["payment_amount"] = None

            else:

                analysis = analyze_legal_content(full_text)

                case_data["outcome"] = analysis["outcome"]
                case_data["payment_found"] = analysis["payment_found"]
                case_data["payment_amount"] = analysis["payment_amount"]

        except Exception as e:

            print(f"⚠️ [Enrich] Error: {e}")

            case_data["court"] = None
            case_data["outcome"] = "unknown"
            case_data["payment_found"] = 0
            case_data["payment_amount"] = None

        finally:
            await page.close()

        return case_data


# =========================
# MAIN
# =========================

async def main():
    # 1. Generate Search URLs
    urls = generate_courtlistener_urls(SEARCH_QUERY, NUM_PAGES)
    print(f"🚀 Phase 1: Scanning {len(urls)} search pages...\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        )

        # ==========================
        # STEP 1: GET LIST OF CASES
        # ==========================
        tasks = [with_retries(scrape_search_page, context, url) for url in urls]
        search_results = await asyncio.gather(*tasks)
        
        # Flatten list
        cases_to_enrich = []
        for res in search_results:
            if res:
                cases_to_enrich.extend(res)

        print(f"\n📋 Found {len(cases_to_enrich)} cases. Starting Phase 2 (Enrichment)...\n")

        # ==========================
        # STEP 2: ENRICH CASES
        # ==========================
        enrich_tasks = [with_retries(enrich_case_details, context, case) for case in cases_to_enrich]
        final_data = await asyncio.gather(*enrich_tasks)

        await browser.close()

    # ==========================
    # STEP 3: SAVE CSV
    # ==========================
    if final_data:
        df_out = pd.DataFrame(final_data)
        
        required_cols = [
            "case_name", "docket_number", "date_filed", 
            "court", "payment_found", "payment_amount", "outcome", "url"
        ]
        
        for col in required_cols:
            if col not in df_out.columns:
                df_out[col] = None 

        df_out = df_out[required_cols]
        
        df_out.to_csv(OUTPUT_FILE, index=False, encoding='utf-8')
        print(f"\n✅ Saved enriched data to {OUTPUT_FILE}")
        
        print(f"Stats: Payments found in {df_out['payment_found'].sum()} cases.")
        print(f"Stats: Outcome determined for {df_out['outcome'].notna().sum()} cases.")
    else:
        print("⚠️ No data found.")


if __name__ == "__main__":
    asyncio.run(main())