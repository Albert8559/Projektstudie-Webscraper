import pandas as pd
import asyncio
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "courtlistener_enriched.csv"

# Concurrency settings
# We keep concurrency moderate (3) to respect the server
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
    
    # Default values
    result = {
        "outcome": None,  # 1 = Win/Success, 0 = Loss
        "payment_found": 0,
        "payment_amount": None
    }

    # -----------------------------
    # 1. DETERMINE OUTCOME (Win/Loss)
    # -----------------------------
    # We look for strong indicators of who won.
    win_signals = [
        "judgment for plaintiff", 
        "plaintiff prevailed", 
        "plaintiff wins", 
        "granted in part", 
        "judgment is entered in favor of plaintiff",
        "defendant is liable"
    ]
    
    loss_signals = [
        "judgment for defendant", 
        "defendant prevailed", 
        "dismissed with prejudice", 
        "dismissed in its entirety",
        "judgment for defendant is entered",
        "plaintiff takes nothing"
    ]

    # Simple scoring based on keyword presence
    win_score = sum(1 for phrase in win_signals if phrase in text_lower)
    loss_score = sum(1 for phrase in loss_signals if phrase in text_lower)

    # Fallback: Look for "granted" vs "denied" if specific judgment phrases aren't found
    if win_score == 0 and loss_score == 0:
        # This is a naive heuristic for appeals or motions
        # Counting "granted" usually implies the requester (often plaintiff) got what they wanted
        # Counting "denied" implies rejection.
        # Note: This is imperfect without knowing WHO filed the motion, but it's a proxy.
        granted_count = text_lower.count("granted")
        denied_count = text_lower.count("denied")
        if granted_count > denied_count:
            result["outcome"] = 1
        elif denied_count > granted_count:
            result["outcome"] = 0
    else:
        if win_score > loss_score:
            result["outcome"] = 1
        elif loss_score > win_score:
            result["outcome"] = 0

    # -----------------------------
    # 2. EXTRACT PAYMENT (DAMAGES)
    # -----------------------------
    # Instead of regex on the whole text (risky), we split into sentences
    # and only extract money from sentences containing 'damage' or 'award' or 'cost'.
    
    # Regex to find currency: $10, $10.00, $1.5 million, etc.
    # We capture the number and the word (million/billion) if present.
    money_pattern = re.compile(r'\$\s?[\d,]+(?:\.\d+)?(?:\s*(million|billion|thousand))?', re.IGNORECASE)
    
    # Split text into sentences (naive split by period, usually sufficient for this heuristic)
    sentences = text.split('. ')
    
    payment_sentences = []
    
    for sentence in sentences:
        s_lower = sentence.lower()
        # Check context keywords
        if any(k in s_lower for k in ["damage", "award", "cost", "fee", "settlement", "attorney"]):
            matches = money_pattern.findall(sentence)
            if matches:
                # We found money in a sentence about damages/costs
                payment_sentences.append((sentence, matches))

    if payment_sentences:
        result["payment_found"] = 1
        # Just taking the first significant amount found
        # cleaning up the match: re.findall returns tuples if groups exist
        first_match = payment_sentences[0][1][0] 
        if isinstance(first_match, tuple):
            # Reconstruct the string if groups were captured
            amount_str = re.search(r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand))?', payment_sentences[0][0]).group(0)
        else:
            amount_str = first_match
            
        result["payment_amount"] = amount_str.replace(" ", "") # Normalize whitespace

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
        # If we don't have a URL, we can't enrich
        if not case_data.get("url"):
            return case_data

        page = await context.new_page()
        
        try:
            url = case_data["url"]
            print(f"🧐 [Enrich] Processing: {case_data['case_name'][:50]}...")
            
            await page.goto(url, timeout=60000)
            # Wait for main content to load
            await page.wait_for_selector("div.row.content", timeout=30000)

            # -----------------------------
            # 1. EXTRACT COURT NAME
            # -----------------------------
            try:
                # Selector: #caption-square > h4.case-court
                court_elem = page.locator("h4.case-court").first
                if await court_elem.count() > 0:
                    case_data["court"] = await court_elem.inner_text()
                else:
                    case_data["court"] = None
            except:
                case_data["court"] = None

            # -----------------------------
            # 2. EXTRACT LEGAL TEXT (ARTICLE)
            # -----------------------------
            # We grab the text of the main opinion/article
            try:
                # Locate the article tag containing the opinion
                article_text = ""
                # Sometimes the content is in an <article>, sometimes just a div.
                # We try to get the largest text block in the main document area.
                content_locator = page.locator("div.row.content")
                
                # Get text, strip excessive whitespace
                full_text = await content_locator.inner_text()
                
                if full_text:
                    # Run Heuristic Analysis
                    analysis = analyze_legal_content(full_text)
                    case_data["outcome"] = analysis["outcome"]
                    case_data["payment_found"] = analysis["payment_found"]
                    case_data["payment_amount"] = analysis["payment_amount"]
                else:
                    case_data["outcome"] = None
                    case_data["payment_found"] = 0
                    case_data["payment_amount"] = 0

            except Exception as e:
                print(f"   Error extracting text for analysis: {e}")
                case_data["outcome"] = None
                case_data["payment_found"] = 0
                case_data["payment_amount"] = 0

        except Exception as e:
            print(f"⚠️ [Enrich] Critical error on {url}: {e}")
            # Ensure columns exist even if failed
            case_data["court"] = case_data.get("court")
            case_data["outcome"] = None
            case_data["payment_found"] = 0
            case_data["payment_amount"] = 0

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
        # Create tasks to visit every specific case URL
        enrich_tasks = [with_retries(enrich_case_details, context, case) for case in cases_to_enrich]
        final_data = await asyncio.gather(*enrich_tasks)

        await browser.close()

    # ==========================
    # STEP 3: SAVE CSV
    # ==========================
    if final_data:
        df_out = pd.DataFrame(final_data)
        
        # Ensure all columns exist (handle missing keys gracefully)
        required_cols = [
            "case_name", "docket_number", "date_filed",
            "court", "payment_found", "payment_amount", "outcome", "url"
        ]
        
        for col in required_cols:
            if col not in df_out.columns:
                df_out[col] = None # Add missing columns if extraction failed entirely

        df_out = df_out[required_cols]
        
        df_out.to_csv(OUTPUT_FILE, index=False, encoding='utf-8')
        print(f"\n✅ Saved enriched data to {OUTPUT_FILE}")
        
        # Quick stats
        print(f"Stats: Payments found in {df_out['payment_found'].sum()} cases.")
    else:
        print("⚠️ No data found.")


if __name__ == "__main__":
    asyncio.run(main())