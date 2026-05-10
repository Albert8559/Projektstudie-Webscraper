import pandas as pd
import asyncio
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "results_heuristic.csv"

MAX_CONCURRENT_ENRICH = 3
RETRIES = 3

SEARCH_QUERY = "%22Patent+Marking%22"
BASE_URL = "https://www.courtlistener.com"
NUM_PAGES = 11


# =========================
# ANALYSIS LOGIC (IMPROVED)
# =========================

def analyze_legal_content(text: str):
    """
    Improved heuristics for Outcome and Payment.
    """
    text_lower = text.lower()
    
    result = {
        "outcome": None, 
        "payment_found": 0,
        "payment_amount": None
    }

    # --- 1. OUTCOME (Win/Loss) ---
    # Expanded keyword list for legal outcomes
    win_signals = [
        "judgment for plaintiff", "plaintiff prevailed", "judgment is entered in favor of plaintiff",
        "grant in part", "granted in part", "order granting", "motion to compel granted",
        "finding of infringement", "defendant is liable", "plaintiff wins"
    ]
    
    loss_signals = [
        "judgment for defendant", "defendant prevailed", "dismissed with prejudice", 
        "dismissed in its entirety", "judgment for defendant is entered", 
        "granting defendant's motion", "order granting summary judgment for defendant",
        "no infringement", "non-infringement"
    ]

    # Score counting
    win_score = sum(1 for phrase in win_signals if phrase in text_lower)
    loss_score = sum(1 for phrase in loss_signals if phrase in text_lower)

    # Secondary checks for Appeal outcomes (Affirmed/Reversed)
    # "Affirmed" usually means the previous result stands.
    # "Reversed" means the previous result is flipped.
    if win_score == 0 and loss_score == 0:
        if "affirmed" in text_lower:
            # This is tricky without context, but usually means status quo
            result["outcome"] = 1 # Assuming status quo favors plaintiff initially for now
        elif "reversed" in text_lower:
            result["outcome"] = 0
        elif "remanded" in text_lower:
            result["outcome"] = None # Neutral
    else:
        if win_score > loss_score:
            result["outcome"] = 1
        elif loss_score > win_score:
            result["outcome"] = 0

    # --- 2. PAYMENT (DAMAGES) ---
    # Regex for standard money formats
    money_pattern = re.compile(r'\$\s?[\d,]+(?:\.\d+)?(?:\s*(million|billion|thousand))?', re.IGNORECASE)
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

def generate_urls(query, pages):
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
                print(f"❌ Failed after retries: {e}")
                return None
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            await asyncio.sleep(wait_time)


scrape_sem = asyncio.Semaphore(3)
enrich_sem = asyncio.Semaphore(3)


# =========================
# SCRAPING LOGIC
# =========================

async def scrape_search_page(context, url):
    async with scrape_sem:
        page = await context.new_page()
        results_on_page = []

        try:
            print(f"🔎 Scanning: {url}")
            await page.goto(url, timeout=60000)
            await page.wait_for_selector("article", timeout=30000)
            
            articles = await page.locator("article").all()
            for article in articles:
                data = {}
                try:
                    title_elem = article.locator('h3.bottom.serif a.visitable')
                    if await title_elem.count() > 0:
                        data["case_name"] = await title_elem.inner_text()
                        href = await title_elem.get_attribute("href")
                        data["url"] = BASE_URL + href if href else None
                    else:
                        continue 

                    time_elem = article.locator('div.bottom div.inline-block time').first
                    data["date_filed"] = await time_elem.get_attribute("datetime") if await time_elem.count() > 0 else None

                    docket_elem = article.locator('span.meta-data-value.select-all').first
                    data["docket_number"] = await docket_elem.inner_text() if await docket_elem.count() > 0 else None

                    if data.get("case_name"):
                        results_on_page.append(data)
                except Exception:
                    continue
        except Exception as e:
            print(f"⚠️ Search error {url}: {e}")
        finally:
            await page.close()
        return results_on_page


async def enrich_case_details(context, case_data):
    async with enrich_sem:
        if not case_data.get("url"):
            return case_data

        page = await context.new_page()
        try:
            url = case_data["url"]
            print(f"🧐 Enriching: {case_data['case_name'][:50]}...")
            
            await page.goto(url, timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except:
                pass

            # -----------------------------
            # 1. ROBUST COURT EXTRACTION
            # -----------------------------
            court_name = None
            
            # Method A: Direct Tag
            try:
                court_elem = page.locator("h4.case-court").first
                if await court_elem.count() > 0:
                    court_name = await court_elem.inner_text()
            except:
                pass

            # Method B: Regex Fallback (If Tag missing)
            # Look for patterns like "District Court, D. Massachusetts" or "United States Court of Appeals"
            if not court_name:
                try:
                    body_text = await page.locator("body").inner_text()
                    # Regex: Starts with word, includes "Court", ends with location/state
                    # This is a generic heuristic to catch court names hiding in text
                    potential_courts = re.findall(r'(?:The\s+)?(?:United States\s+)?.*?\s+Court\s+(?:of\s+)?[\w\s,]+(?:District|Circuit|Appeals|Supreme|Appellate)\s+[\w\s,]+', body_text)
                    if potential_courts:
                        court_name = potential_courts[0].strip()
                except:
                    pass
            
            case_data["court"] = court_name

            # -----------------------------
            # 2. ROBUST TEXT EXTRACTION
            # -----------------------------
            # Priority: <article> -> <div class="row content"> -> <div class="col-sm-9">
            selectors = [
                "article",
                "div.row.content", 
                "div.col-sm-9.main.document",
                "div#opinion-content"
            ]

            full_text = ""
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        text = await locator.inner_text()
                        if len(text) > 100:
                            full_text = text
                            break
                except:
                    continue

            if not full_text:
                print(f"   ⚠️ No text content found.")
                case_data["outcome"] = None
                case_data["payment_found"] = 0
                case_data["payment_amount"] = None
            else:
                # Run Improved Heuristics
                analysis = analyze_legal_content(full_text)
                case_data["outcome"] = analysis["outcome"]
                case_data["payment_found"] = analysis["payment_found"]
                case_data["payment_amount"] = analysis["payment_amount"]

        except Exception as e:
            print(f"⚠️ Critical error on {url}: {e}")
            # Ensure defaults
            if "court" not in case_data: case_data["court"] = None
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
    urls = generate_urls(SEARCH_QUERY, NUM_PAGES)
    print(f"🚀 Processing {len(urls)} pages...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

        # Step 1
        tasks = [with_retries(scrape_search_page, context, url) for url in urls]
        search_results = await asyncio.gather(*tasks)
        
        cases_to_enrich = []
        for res in search_results:
            if res: cases_to_enrich.extend(res)

        print(f"\n📋 Found {len(cases_to_enrich)} cases. Enriching...\n")

        # Step 2
        enrich_tasks = [with_retries(enrich_case_details, context, case) for case in cases_to_enrich]
        final_data = await asyncio.gather(*enrich_tasks)

        await browser.close()

    if final_data:
        df_out = pd.DataFrame(final_data)
        cols = ["case_name", "docket_number", "date_filed", "court", "payment_found", "payment_amount", "outcome", "url"]
        for col in cols: 
            if col not in df_out.columns: df_out[col] = None
            
        df_out = df_out[cols]
        df_out.to_csv(OUTPUT_FILE, index=False, encoding='utf-8')
        print(f"\n✅ Saved to {OUTPUT_FILE}")
        print(f"Stats: Payments in {df_out['payment_found'].sum()} cases.")

if __name__ == "__main__":
    asyncio.run(main())