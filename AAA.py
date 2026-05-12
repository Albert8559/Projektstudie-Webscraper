import pandas as pd
import asyncio
import random
import re
import os
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "results_final_comp_3.csv"

# Concurrency settings
MAX_CONCURRENT_SCRAPE = 1  
MAX_CONCURRENT_ENRICH = 1  
RETRIES = 3

SEARCH_QUERY = "%22Patents%22"
BASE_URL = "https://www.courtlistener.com"
NUM_PAGES = 25

# Debug folder
DEBUG_FOLDER = "debug_failures"
if not os.path.exists(DEBUG_FOLDER):
    os.makedirs(DEBUG_FOLDER)


# =========================
# ANALYSIS LOGIC
# =========================

def analyze_legal_content(text: str):
    text_lower = text.lower()
    result = {
        "outcome": None, 
        "payment_found": 0,
        "payment_amount": None
    }

    # --- 1. OUTCOME ---
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

    win_score = sum(1 for phrase in win_signals if phrase in text_lower)
    loss_score = sum(1 for phrase in loss_signals if phrase in text_lower)

    if win_score == 0 and loss_score == 0:
        if "affirmed" in text_lower:
            result["outcome"] = 1 
        elif "reversed" in text_lower:
            result["outcome"] = 0
        elif "remanded" in text_lower:
            result["outcome"] = None 
    else:
        if win_score > loss_score:
            result["outcome"] = 1
        elif loss_score > win_score:
            result["outcome"] = 0

    # --- 2. PAYMENT ---
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
                break 

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
                print(f" Failed after {RETRIES} retries: {e}")
                return None
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            await asyncio.sleep(wait_time)

scrape_sem = asyncio.Semaphore(MAX_CONCURRENT_SCRAPE)
enrich_sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICH)


# =========================
# SCRAPING LOGIC
# =========================

async def scrape_search_page(context, url):
    async with scrape_sem:
        page = await context.new_page()
        
        results_on_page = []

        try:
            print(f" Scanning: {url}")
            await page.goto(url, timeout=60000)
            
            # --- MANDATORY 15 SECOND PAUSE ---
            print("    Waiting 15 seconds to solve Captcha (if present)...")
            await asyncio.sleep(9)
            # -------------------------------

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
            print(f" Search error {url}: {e}")
        finally:
            await page.close()
        return results_on_page


async def enrich_case_details(context, case_data):
    async with enrich_sem:
        if not case_data.get("url"):
            return case_data

        page = await context.new_page()
        
        safe_name = case_data.get('case_name', 'Unknown')[:30].replace("/", "-").replace("\\", "-")
        
        try:
            url = case_data["url"]
            print(f" [Enrich] Processing: {safe_name}...")
            
            await page.goto(url, timeout=60000)
            
            # --- MANDATORY 15 SECOND PAUSE (REQUESTED) ---
            print("     Waiting 15 seconds to solve Captcha (if present)...")
            await asyncio.sleep(9)
            # ----------------------------------------------

            await page.wait_for_load_state("networkidle", timeout=45000)

            # 1. EXTRACT COURT NAME
            try:
                court_elem = page.locator("h4.case-court, h4.docket-court").first
                if await court_elem.count() > 0:
                    case_data["court"] = await court_elem.inner_text()
            except:
                case_data["court"] = None

            # 2. EXTRACT TEXT
            full_text = ""
            content_found = False
            
            selectors = [
                "div#opinion-content",
                "div#harvard-text",
                "div.section-opinion-content", 
                "div.main-document",
                "div.opinion-text",
                "article"
            ]

            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        await page.wait_for_selector(selector, state="attached", timeout=2000)
                        text = await locator.inner_text()
                        if len(text) > 500:
                            full_text = text
                            content_found = True
                            print(f"    Found text via: {selector}")
                            break
                except Exception:
                    continue

            # 3. NUCLEAR FALLBACK
            if not content_found:
                print(f"    Specific selectors failed. Trying body text fallback...")
                try:
                    body_text = await page.locator("body").inner_text()
                    if len(body_text) > 1000:
                        full_text = body_text
                        content_found = True
                        print(f"    Found text via Body fallback.")
                    else:
                        pass 
                except Exception:
                    pass

            # 4. FAILURE DIAGNOSTICS
            if not content_found:
                print(f"    FAILED to extract text for {safe_name}. Saving screenshot...")
                try:
                    timestamp = datetime.now().strftime('%H%M%S')
                    screenshot_path = f"{DEBUG_FOLDER}/{safe_name}_{timestamp}.png"
                    await page.screenshot(path=screenshot_path, full_page=True)
                except Exception:
                    pass
                
                case_data["outcome"] = None
                case_data["payment_found"] = 0
                case_data["payment_amount"] = None
                return case_data

            # 5. RUN HEURISTICS
            analysis = analyze_legal_content(full_text)
            case_data["outcome"] = analysis["outcome"]
            case_data["payment_found"] = analysis["payment_found"]
            case_data["payment_amount"] = analysis["payment_amount"]

        except Exception as e:
            print(f" [Enrich] Critical error on {url}: {e}")
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
    urls = generate_urls(SEARCH_QUERY, NUM_PAGES)
    print(f" Processing {len(urls)} pages...")
    print("  BROWSER WILL OPEN VISIBLY.")
    print(" THE SCRIPT PAUSES FOR 15 SECONDS ON EVERY PAGE.")
    print(" USE THIS TIME TO SOLVE CAPTCHAS IF THEY APPEAR.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, 
            args=['--start-maximized']
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
            timezone_id='America/New_York'
        )

        tasks = [with_retries(scrape_search_page, context, url) for url in urls]
        search_results = await asyncio.gather(*tasks)
        
        cases_to_enrich = []
        for res in search_results:
            if res: cases_to_enrich.extend(res)

        print(f"\n Found {len(cases_to_enrich)} cases. Enriching...\n")

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
        print(f"\n Saved to {OUTPUT_FILE}")
        print(f"Stats: Payments in {df_out['payment_found'].sum()} cases.")

if __name__ == "__main__":
    asyncio.run(main())