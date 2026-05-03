import pandas as pd
import re
import asyncio
from datetime import datetime

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "results_enriched.csv"

MAX_CONCURRENT_ENRICH = 5
RETRIES = 2


# =========================
# HELPERS
# =========================

def is_blocked(text: str) -> bool:
    signals = ["captcha", "verify you are human", "access denied", "unusual traffic", "sorry"]
    text = text.lower()
    return any(s in text for s in signals)


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
# SCRAPING - CORRECT PAGINATION
# =========================

async def scrape_search_pages(context, query, max_pages=10):
    """
    Properly scrape Google Patents by clicking through pages
    and extracting patent IDs from data-docid attributes.
    """
    page = await context.new_page()
    all_patents = set()
    
    try:
        # Initial search URL
        url = f"https://patents.google.com/?q={query}&oq={query}"
        print(f"🔎 Starting search: {url}")
        
        await page.goto(url, timeout=60000)
        
        # Wait for results container
        try:
            await page.wait_for_selector('#searchResults', timeout=20000)
        except:
            await page.wait_for_selector('search-result-item', timeout=20000)
        
        await asyncio.sleep(2)  # Let dynamic content load
        
        for page_num in range(1, max_pages + 1):
            print(f"\n📄 Processing page {page_num}...")
            
            # Check for block
            content = await page.content()
            if is_blocked(content):
                print(f"🚫 Blocked on page {page_num}")
                break
            
            # ✅ METHOD 1: Extract from data-docid attributes (MOST RELIABLE)
            patents_on_page = set()
            
            result_items = await page.query_selector_all('search-result-item')
            print(f"   Found {len(result_items)} result items")
            
            for item in result_items:
                try:
                    doc_id = await item.get_attribute('data-docid')
                    if doc_id:
                        # Clean up the patent number
                        doc_id = doc_id.strip()
                        # Remove prefix like "patent/" if present
                        if "/" in doc_id:
                            doc_id = doc_id.split("/")[-1]
                        patents_on_page.add(doc_id)
                except:
                    pass
            
            # ✅ METHOD 2: Extract from links as fallback
            if not patents_on_page:
                links = await page.query_selector_all('a[href*="/patent/"]')
                for link in links:
                    try:
                        href = await link.get_attribute('href')
                        if href:
                            # Extract patent number from URL
                            match = re.search(r'/patent/([^/?]+)', href)
                            if match:
                                patent = match.group(1)
                                # Remove publication kind code if present (e.g., A1, B2)
                                patent = re.sub(r'[A-Z]\d$', '', patent)
                                patents_on_page.add(patent)
                    except:
                        pass
            
            # ✅ METHOD 3: Regex from page content as last resort
            if not patents_on_page:
                PATENT_REGEX = re.compile(
                    r'(?:US|EP|WO|CN|JP|DE|GB|FR)\s?(\d{4,})(?:\s?[A-Z]\d?)?'
                )
                text = await page.inner_text('body')
                matches = PATENT_REGEX.findall(text)
                for m in matches:
                    patents_on_page.add(f"US{m}")  # Assuming US if not specified
            
            new_count = len(patents_on_page - all_patents)
            all_patents.update(patents_on_page)
            
            print(f"   ✅ New patents this page: {new_count}")
            print(f"   📊 Total unique patents: {len(all_patents)}")
            
            # ✅ CLICK NEXT BUTTON (CORRECT PAGINATION METHOD)
            if page_num < max_pages:
                next_clicked = False
                
                # Try various selectors for the Next button
                next_selectors = [
                    'a[aria-label="Next"]',
                    'button[aria-label="Next"]',
                    'a.next-button',
                    'button:has-text("Next")',
                    'a:has-text("Next")',
                    '[data-chip-id="next"]',
                    '#pagination a:last-child',
                    'a.pagination-next',
                ]
                
                for selector in next_selectors:
                    try:
                        next_btn = await page.query_selector(selector)
                        if next_btn:
                            # Check if button is disabled
                            is_disabled = await next_btn.get_attribute('disabled')
                            class_name = await next_btn.get_attribute('class') or ""
                            
                            if is_disabled or 'disabled' in class_name:
                                print(f"   🛑 Next button is disabled, no more pages")
                                break
                            
                            # Scroll to button first
                            await next_btn.scroll_into_view_if_needed()
                            await asyncio.sleep(0.5)
                            
                            # Click next
                            await next_btn.click()
                            next_clicked = True
                            
                            # Wait for new results to load
                            await asyncio.sleep(3)
                            
                            # Wait for loading spinner to disappear
                            try:
                                await page.wait_for_selector('.loading, [data-loading]', 
                                                            state='hidden', timeout=5000)
                            except:
                                pass
                            
                            break
                    except Exception as e:
                        continue
                
                if not next_clicked:
                    print(f"   🛑 Could not find/click Next button, stopping")
                    break
                
                # Additional delay to avoid rate limiting
                await asyncio.sleep(2)
    
    except Exception as e:
        print(f"⚠️ Scraping error: {e}")
    
    await page.close()
    return all_patents


# =========================
# EXPIRATION EXTRACTION
# =========================

async def extract_expiration(page):
    try:
        # Look for expiration date in various places
        selectors = [
            'section[itemprop="legalStatus"] time',
            'div.legal-status time',
            'tr:has-text("Expiration") td',
            'tr:has-text("Expected Expiration") td',
            '[data-value="Expiration"]',
        ]
        
        for selector in selectors:
            elements = page.locator(selector)
            count = await elements.count()
            
            for i in range(count):
                text = await elements.nth(i).text_content()
                if text:
                    text = text.strip()
                    # Match date formats: YYYY-MM-DD, MM/DD/YYYY, etc.
                    match = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})', text)
                    if match:
                        return match.group(1)

        return None

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
        
        # Wait for content - try multiple selectors
        try:
            await page.wait_for_selector('dd[itemprop="assigneeCurrent"], dd[itemprop="assigneeOriginal"], .title', 
                                        timeout=10000)
        except:
            pass
        
        await asyncio.sleep(1)

        # ASSIGNEE
        for prop in ["assigneeCurrent", "assigneeOriginal"]:
            if not data["assignee"]:
                try:
                    val = await page.locator(f'dd[itemprop="{prop}"]').first.text_content()
                    if val:
                        data["assignee"] = val.strip()
                except:
                    pass

        # STATUS
        try:
            val = await page.locator('span[itemprop="legalStatus"]').first.text_content()
            if val:
                data["status"] = val.strip()
        except:
            pass

        # FILING DATE
        try:
            val = await page.locator('time[itemprop="filingDate"]').first.text_content()
            if val:
                data["filing_date"] = val.strip()
        except:
            pass

        # ABSTRACT
        try:
            val = await page.locator('section[itemprop="abstract"], .abstract').first.inner_text()
            if val:
                data["abstract"] = val.strip()[:1000]
        except:
            pass

        # EXPIRATION
        expiry = await extract_expiration(page)
        if expiry:
            data["expiration_date"] = expiry
        elif data["filing_date"]:
            data["expiration_date"] = compute_expiry(data["filing_date"])

    except Exception as e:
        print(f"⚠️ Enrich error {patent_number}: {e}")

    await page.close()
    return data


enrich_sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICH)

async def enrich_worker(context, patent):
    async with enrich_sem:
        result = await with_retries(enrich_patent_playwright, context, patent)
        if result:
            print(f"   📝 {patent}")
        return result


# =========================
# MAIN
# =========================

async def main():
    QUERY = "siemens"
    NUM_PAGES = 10

    print(f"🚀 Scraping Google Patents for: '{QUERY}'")
    print(f"   Max pages: {NUM_PAGES}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )

        # STEP 1: SCRAPE SEARCH PAGES (SEQUENTIAL)
        print("=" * 50)
        print("STEP 1: SCRAPING SEARCH PAGES")
        print("=" * 50)
        
        all_patents = await scrape_search_pages(context, QUERY, NUM_PAGES)

        print(f"\n🧾 Total unique patents found: {len(all_patents)}\n")

        if not all_patents:
            print("❌ No patents found!")
            await browser.close()
            return

        # STEP 2: ENRICH PATENTS (PARALLEL)
        print("=" * 50)
        print("STEP 2: ENRICHING PATENTS")
        print("=" * 50)
        
        enrich_tasks = [
            enrich_worker(context, patent)
            for patent in sorted(all_patents)
        ]

        enriched_results = await asyncio.gather(*enrich_tasks)
        enriched_results = [r for r in enriched_results if r]

        await browser.close()

    # SAVE OUTPUT
    if enriched_results:
        df_out = pd.DataFrame(enriched_results)
        df_out.to_csv(OUTPUT_FILE, index=False)
        print(f"\n{'=' * 50}")
        print(f"✅ DONE: {len(enriched_results)} patents saved to {OUTPUT_FILE}")
        print(f"{'=' * 50}")
    else:
        print("\n❌ No results to save.")


if __name__ == "__main__":
    asyncio.run(main())