import pandas as pd
import asyncio
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
import os # Add this to imports

# Ensure a folder exists for debugging
if not os.path.exists("debug_failures"):
    os.makedirs("debug_failures")
# =========================
# CONFIG
# =========================
OUTPUT_FILE = "results_final.csv"

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


import os # Add this to imports

# Ensure a folder exists for debugging
if not os.path.exists("debug_failures"):
    os.makedirs("debug_failures")

async def enrich_case_details(context, case_data):
    async with enrich_sem:
        if not case_data.get("url"):
            return case_data

        page = await context.new_page()
        safe_name = case_data.get('case_name', 'Unknown')[:30].replace("/", "-")
        
        try:
            url = case_data["url"]
            print(f" [Enrich] Processing: {safe_name}...")
            
            await page.goto(url, timeout=60000)
            
            # CRITICAL: Wait for 'networkidle' to ensure JS rendering is mostly done
            # Some CL pages render text heavily with React/Vue
            await page.wait_for_load_state("networkidle", timeout=45000)
            
            # -----------------------------------------
            # 1. Extract Court Name
            # -----------------------------------------
            try:
                court_elem = page.locator("h4.case-court, h4.docket-court").first
                if await court_elem.count() > 0:
                    case_data["court"] = await court_elem.inner_text()
            except:
                case_data["court"] = None

            # -----------------------------------------
            # 2. Extract Text (The "Nuclear" Strategy)
            # -----------------------------------------
            full_text = ""
            
            # LIST OF SPECIFIC SELECTORS (From specific to general)
            selectors = [
                "div#opinion-content",
                "div#harvard-text",
                "div.section-opinion-content", 
                "div.main-document",
                "div.opinion-text",
                "article"
            ]

            content_found = False
            
            # Try specific selectors first
            for selector in selectors:
                try:
                    # Check if element exists and is visible
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        # Wait a bit for it to fill with text
                        await page.wait_for_selector(selector, state="attached", timeout=2000)
                        
                        text = await locator.inner_text()
                        
                        # Heuristic: If text is substantial (> 500 chars), use it.
                        # This prevents grabbing empty divs or just "Opinion" headers.
                        if len(text) > 500:
                            full_text = text
                            content_found = True
                            print(f"   ✅ Found text via selector: {selector}")
                            break
                except Exception:
                    continue

            # -----------------------------------------
            # 3. THE NUCLEAR FALLBACK (If specific selectors failed)
            # -----------------------------------------
            if not content_found:
                print(f"   ⚠️ Specific selectors failed. Falling back to <body>...")
                try:
                    # Grab the whole body text. It includes nav/footer, but it's better than nothing.
                    body_text = await page.locator("body").inner_text()
                    
                    # Filter: If the body text is very short, we are likely on a Docket page or Error page.
                    if len(body_text) > 1000:
                        full_text = body_text
                        content_found = True
                        print(f"   ✅ Found text via Body (Nuclear Option).")
                    else:
                        # Text is too short. Something is wrong (PDF, Login, Empty).
                        pass 
                except Exception:
                    pass

            # -----------------------------------------
            # 4. FAILURE DIAGNOSTICS (If we still have no text)
            # -----------------------------------------
            if not content_found:
                print(f"   ❌ FAILED to extract text for {safe_name}. Saving screenshot...")
                try:
                    # Save screenshot so you can see WHY it failed
                    screenshot_path = f"debug_failures/{safe_name}_{datetime.now().strftime('%H%M%S')}.png"
                    await page.screenshot(path=screenshot_path, full_page=True)
                    print(f"   💾 Saved debug screenshot to {screenshot_path}")
                except:
                    pass
                
                # Set defaults
                case_data["outcome"] = None
                case_data["payment_found"] = 0
                case_data["payment_amount"] = None
                return case_data

            # -----------------------------------------
            # 5. Run Analysis
            # -----------------------------------------
            analysis = analyze_legal_content(full_text)
            case_data["outcome"] = analysis["outcome"]
            case_data["payment_found"] = analysis["payment_found"]
            case_data["payment_amount"] = analysis["payment_amount"]

        except Exception as e:
            print(f" [Enrich] CRITICAL ERROR on {url}: {e}")
            # Ensure keys exist
            case_data["court"] = case_data.get("court")
            case_data["outcome"] = None
            case_data["payment_found"] = 0
            case_data["payment_amount"] = None
        finally:
            await page.close()
            
        return case_data

# Helper to avoid repeating court logic
async def get_court_name(page):
    try:
        court_elem = page.locator("h4.case-court").first
        if await court_elem.count() > 0:
            return await court_elem.inner_text()
    except:
        pass
    return None

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