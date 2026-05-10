import pandas as pd
import asyncio
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "courtlistener_enriched_2.csv"

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
    Analyzes the legal text to determine outcome and payments.
    Returns a dict with 'outcome' (0/1), 'payment_found' (0/1), and 'payment_amount'.
    """
    text_lower = text.lower()
    
    result = {
        "outcome": None, 
        "payment_found": 0,
        "payment_amount": None
    }

    # -----------------------------
    # 1. DETERMINE OUTCOME
    # -----------------------------
    win_signals = [
        "judgment for plaintiff", "plaintiff prevailed", "plaintiff wins", 
        "granted in part", "judgment is entered in favor of plaintiff", "defendant is liable"
    ]
    loss_signals = [
        "judgment for defendant", "defendant prevailed", "dismissed with prejudice", 
        "dismissed in its entirety", "judgment for defendant is entered", "plaintiff takes nothing"
    ]

    win_score = sum(1 for phrase in win_signals if phrase in text_lower)
    loss_score = sum(1 for phrase in loss_signals if phrase in text_lower)

    if win_score > loss_score:
        result["outcome"] = 1
    elif loss_score > win_score:
        result["outcome"] = 0

    # -----------------------------
    # 2. EXTRACT PAYMENT
    # -----------------------------
    money_pattern = re.compile(r'\$\s?[\d,]+(?:\.\d+)?(?:\s*(million|billion|thousand))?|($)', re.IGNORECASE)
    sentences = text.split('. ')
    
    for sentence in sentences:
        s_lower = sentence.lower()
        if any(k in s_lower for k in ["damage", "award", "cost", "fee", "settlement", "attorney"]):
            matches = money_pattern.findall(sentence)
            if matches:
                result["payment_found"] = 1
                match_str = re.search(r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand))?', sentence).group(0)
                result["payment_amount"] = match_str.replace(" ", "")
                break # Just take the first one

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
            
            # ROBUST WAIT: Wait for network to be mostly idle, rather than a specific selector
            # This prevents timeouts on pages with different layouts (PDFs, old HTML)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except:
                # If networkidle fails, we just proceed anyway; maybe the site is slow
                pass

            # -----------------------------
            # 1. EXTRACT COURT NAME
            # -----------------------------
            try:
                # Try finding the court in the standard header area
                court_elem = page.locator("h4.case-court").first
                if await court_elem.count() > 0:
                    case_data["court"] = await court_elem.inner_text()
                else:
                    # Fallback: Look in the body if header structure is different
                    # Sometimes court is in a generic meta div
                    meta_court = page.locator("dd:has-text('Court') + dd").first
                    if await meta_court.count() > 0:
                         case_data["court"] = await meta_court.inner_text()
                    else:
                         case_data["court"] = None
            except:
                case_data["court"] = None

            # -----------------------------
            # 2. EXTRACT TEXT (FALLBACK SYSTEM)
            # -----------------------------
            full_text = ""
            
            # List of selectors to try, ordered by likelihood of containing the main opinion text
            selectors = [
                "div#opinion-content",       # Most common modern layout
                "div.col-sm-9.main.document",# The layout you specified
                "article",                    # Semantic HTML tag
                "div.opinion-text",           # Older layout variation
                "div.main-content"            # Fallback generic
            ]

            content_found = False
            for selector in selectors:
                try:
                    # Check if element exists
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        text = await locator.inner_text()
                        # Sanity check: If text is very short, it might just be a header/spinner
                        if len(text) > 200: 
                            full_text = text
                            content_found = True
                            break
                except:
                    continue

            if not content_found:
                print(f"   ⚠️ Could not find main text content for {case_data['case_name'][:30]} (Likely PDF or Unsupported Layout)")
                case_data["outcome"] = None
                case_data["payment_found"] = 0
                case_data["payment_amount"] = None
            else:
                # Run Heuristic Analysis
                analysis = analyze_legal_content(full_text)
                case_data["outcome"] = analysis["outcome"]
                case_data["payment_found"] = analysis["payment_found"]
                case_data["payment_amount"] = analysis["payment_amount"]

        except Exception as e:
            print(f"⚠️ [Enrich] Critical error on {url}: {e}")
            # Ensure columns exist
            case_data["court"] = case_data.get("court")
            case_data["outcome"] = None
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
            "case_name", "docket_number", "date_filed", "url", 
            "court", "payment_found", "payment_amount", "outcome"
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