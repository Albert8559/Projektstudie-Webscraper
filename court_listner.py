import pandas as pd
import asyncio
import random
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "courtlistener_results.csv"

# CourtListener can be sensitive to high traffic. 
# We keep concurrency moderate to avoid immediate 429 (Too Many Requests) errors.
MAX_CONCURRENT_SCRAPES = 3
RETRIES = 3

# Search Parameters
SEARCH_QUERY = "%22Patent+Marking%22"
BASE_URL = "https://www.courtlistener.com"
NUM_PAGES = 11


# =========================
# HELPERS
# =========================

def generate_courtlistener_urls(query, pages):
    """
    Generates paginated URLs for CourtListener search results.
    """
    url_template = (
        f"{BASE_URL}/?q={query}"
        "&type=o&order_by=dateFiled+desc&stat_Published=on&page={page_num}"
    )
    return [url_template.format(page_num=i) for i in range(1, pages + 1)]


# =========================
# RETRY WRAPPER
# =========================

async def with_retries(coro, *args, **kwargs):
    """Retries a coroutine if it fails, with exponential backoff."""
    for attempt in range(RETRIES + 1):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            if attempt == RETRIES:
                print(f"❌ Failed after {RETRIES} retries: {e}")
                return None
            # Exponential backoff: 2s, 4s, 8s...
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️ Attempt {attempt + 1} failed. Retrying in {wait_time:.2f}s...")
            await asyncio.sleep(wait_time)


# =========================
# SEMAPHORE
# =========================

scrape_sem = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)


# =========================
# SCRAPING
# =========================

async def scrape_courtlistener_page(context, url):
    """
    Scrapes a single page of CourtListener results.
    Extracts Case Name, Date, Docket #, and URL for every article found.
    """
    async with scrape_sem:
        page = await context.new_page()
        results_on_page = []

        try:
            print(f"🔎 Scraping: {url}")
            
            # Navigate with a generous timeout
            await page.goto(url, timeout=60000)
            
            # Wait for the article list to load (specific to CourtListener)
            await page.wait_for_selector("article", timeout=30000)

            # Get all article elements on the page
            articles = await page.locator("article").all()
            print(f"   Found {len(articles)} articles on this page.")

            for article in articles:
                data = {}

                # -----------------------------
                # 1. CASE NAME & CITATION
                # -----------------------------
                # Target: h3.bottom.serif > a.visitable
                try:
                    # We use inner_text() to get the clean text including the citation part (e.g., "(D.N.H. 2025)")
                    title_elem = article.locator('h3.bottom.serif a.visitable')
                    if await title_elem.count() > 0:
                        data["case_name"] = await title_elem.inner_text()
                        # Extract the link href as well for reference
                        data["url"] = await title_elem.get_attribute("href")
                        if data["url"] and not data["url"].startswith("http"):
                            data["url"] = BASE_URL + data["url"]
                    else:
                        data["case_name"] = None
                        data["url"] = None
                except Exception as e:
                    print(f"      Error extracting title: {e}")
                    data["case_name"] = None

                # -----------------------------
                # 2. DATE (DATETIME)
                # -----------------------------
                # Target: div.bottom > div.inline-block > time
                # Note: 'inline-block' is usually a single class name in CSS, but we look for the time tag.
                try:
                    # Locate time element relative to the article
                    time_elem = article.locator('div.bottom div.inline-block time').first
                    if await time_elem.count() > 0:
                        data["date_filed"] = await time_elem.get_attribute("datetime")
                    else:
                        data["date_filed"] = None
                except Exception as e:
                    print(f"      Error extracting date: {e}")
                    data["date_filed"] = None

                # -----------------------------
                # 3. DOCKET NUMBER
                # -----------------------------
                # Target: span.meta-data-value.select-all
                try:
                    docket_elem = article.locator('span.meta-data-value.select-all').first
                    if await docket_elem.count() > 0:
                        data["docket_number"] = await docket_elem.inner_text()
                    else:
                        data["docket_number"] = None
                except Exception as e:
                    print(f"      Error extracting docket: {e}")
                    data["docket_number"] = None

                # Only add if we found at least a case name
                if data.get("case_name"):
                    results_on_page.append(data)

        except Exception as e:
            print(f"⚠️ Critical error scraping page {url}: {e}")
        
        finally:
            await page.close()
            
        return results_on_page


# =========================
# MAIN
# =========================

async def main():
    # 1. Generate URLs
    urls = generate_courtlistener_urls(SEARCH_QUERY, NUM_PAGES)
    print(f"🚀 Processing {len(urls)} pages for query: 'Patent Marking'\n")

    # 2. Launch Browser
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # Set a realistic User Agent to avoid basic bot detection
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            # CourtListener sets cookies strictly; we accept all to ensure search works
            accept_downloads=False,
        )

        # 3. Run Scraping Tasks
        # We use with_retries to handle network blips or temporary blocks
        tasks = [with_retries(scrape_courtlistener_page, context, url) for url in urls]
        
        # Gather results from all pages
        list_of_results = await asyncio.gather(*tasks)

        await browser.close()

    # 4. Flatten and Process Data
    # list_of_results is a list of lists. We need to flatten it.
    final_data = []
    for page_results in list_of_results:
        if page_results:
            final_data.extend(page_results)

    print(f"\n📊 Total cases extracted: {len(final_data)}")

    # 5. Save to CSV
    if final_data:
        df_out = pd.DataFrame(final_data)
        
        # Optional: Reorder columns for readability
        cols = ["case_name", "docket_number", "date_filed", "url"]
        df_out = df_out[cols]
        
        df_out.to_csv(OUTPUT_FILE, index=False, encoding='utf-8')
        print(f"✅ Saved results to {OUTPUT_FILE}")
    else:
        print("⚠️ No data extracted. Check selectors or network connection.")


# =========================
# RUN
# =========================

if __name__ == "__main__":
    asyncio.run(main())