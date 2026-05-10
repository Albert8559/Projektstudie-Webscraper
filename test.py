import pandas as pd
import asyncio
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright

# =========================
# CONFIG
# =========================
ASSIGNEE = "Bosch"
MAX_PAGES = 5  # Bosch has many patents; increase this to 50, 100, etc.
OUTPUT_FILE = "results_bosch.csv"

# Concurrency Settings
MAX_CONCURRENT_SEARCH = 3  
MAX_CONCURRENT_ENRICH = 5  
BASE_DELAY = 1.0            # Base delay between requests (seconds)

# =========================
# HELPERS
# =========================

def is_blocked(page_content: str) -> bool:
    """Check if the response indicates a block or CAPTCHA."""
    signals = ["captcha", "verify you are human", "access denied", "unusual traffic", "sorry"]
    content = page_content.lower()
    return any(s in content for s in signals)

def compute_expiry(filing_date):
    """Calculate expiration as Filing + 20 years."""
    if not filing_date:
        return None
    try:
        dt = datetime.strptime(filing_date, "%Y-%m-%d")
        return dt.replace(year=dt.year + 20).strftime("%Y-%m-%d")
    except Exception:
        return None

async def random_delay():
    """Sleep for a random duration to mimic human behavior."""
    await asyncio.sleep(BASE_DELAY + random.random())

# =========================
# SEMAPHORES
# =========================
search_sem = asyncio.Semaphore(MAX_CONCURRENT_SEARCH)
enrich_sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICH)

# =========================
# STEP 1: SCRAPE SEARCH (PARALLEL)
# =========================

async def scrape_search_page(context, base_url, page_num):
    async with search_sem:
        patents = set()
        page = await context.new_page()
        
        # Construct URL for specific assignee page
        # Google Patents structure: /?assignee=...&page=...
        url = f"{base_url}&page={page_num}"
        
        try:
            await random_delay()
            print(f"🔍 Scraping Page {page_num}: {url}")
            
            await page.goto(url, timeout=30000)
            
            # Wait for results to load
            try:
                await page.wait_for_selector('search-result-item', timeout=10000)
            except:
                # If selector not found, check if blocked or no results
                content = await page.content()
                if is_blocked(content):
                    print(f"🚫 Blocked/Captcha on Page {page_num}")
                    return set()
                # Check if there are literally no results
                no_results = await page.query_selector('text("0 results")')
                if no_results:
                    print(f"🛑 Reached end of results at page {page_num}")
                    return set()
                
                print(f"⚠️ No results found (or timed out) on Page {page_num}")
                return set()

            # --- ROBUST EXTRACTION ---
            # Method 1: data-docid (Most reliable)
            result_items = await page.query_selector_all('search-result-item')
            
            for item in result_items:
                try:
                    doc_id = await item.get_attribute('data-docid')
                    if doc_id:
                        # Clean ID
                        doc_id = doc_id.strip()
                        # Handle ID formats like "patent/US12345/A1" -> "US12345/A1"
                        # Sometimes Bosch patents might be DE (Germany) or WO (International)
                        if "/" in doc_id:
                            doc_id = doc_id.split("/")[-1]
                        patents.add(doc_id)
                except Exception:
                    pass

            # Fallback if data-docid fails
            if not patents:
                links = await page.query_selector_all('a[href*="/patent/"]')
                for link in links:
                    try:
                        href = await link.get_attribute('href')
                        if href and "/patent/" in href:
                            pid = href.split("/patent/")[1].split("?")[0].split("/")[0]
                            patents.add(pid)
                    except Exception:
                        pass

            print(f"   ✅ Page {page_num}: Found {len(patents)} patents")
            
        except Exception as e:
            print(f"⚠️ Error scraping page {page_num}: {e}")
        finally:
            await page.close()
            
        return patents

# =========================
# STEP 2: ENRICH DETAILS
# =========================

async def enrich_patent(context, patent_id):
    async with enrich_sem:
        await random_delay()
        page = await context.new_page()
        
        data = {
            "patent_id": patent_id,
            "jurisdiction": patent_id[:2] if len(patent_id) > 2 else "Unknown",
            "assignee": None,
            "status": None,
            "filing_date": None,
            "expiration_date": None,
            "title": None,
            "abstract": None,
        }

        try:
            url = f"https://patents.google.com/patent/{patent_id}"
            await page.goto(url, timeout=3000)
            
            # Wait for key metadata
            try:
                await page.wait_for_selector('h1', timeout=3000) 
            except:
                pass

            # 1. TITLE
            try:
                data["title"] = (await page.locator('h1').first.inner_text()).strip()
            except:
                pass

            # 2. ASSIGNEE (Try Current, then Original)
            # This is critical for Bosch as it might list subsidiaries
            try:
                data["assignee"] = (await page.locator('dd[itemprop="assigneeCurrent"]').first.text_content()).strip()
            except:
                try:
                    data["assignee"] = (await page.locator('dd[itemprop="assigneeOriginal"]').first.text_content()).strip()
                except:
                    pass

            # 3. STATUS
            try:
                data["status"] = (await page.locator('span[itemprop="legalStatus"]').first.text_content()).strip()
            except:
                pass

            # 4. FILING DATE
            try:
                data["filing_date"] = (await page.locator('time[itemprop="filingDate"]').first.text_content()).strip()
            except:
                pass

            # 5. ABSTRACT
            try:
                abstract_sel = page.locator('section[itemprop="abstract"], .abstract').first
                if await abstract_sel.count() > 0:
                    data["abstract"] = (await abstract_sel.inner_text()).strip()[:500] 
            except:
                pass

            # 6. EXPIRATION
            try:
                # Look for explicit expiration dates
                times = await page.locator('div.legal-status time, tr:has-text("Expiration") time').all_text_contents()
                valid_dates = []
                for t in times:
                    if re.search(r'\d{4}-\d{2}-\d{2}', t):
                        valid_dates.append(t.strip())
                
                if valid_dates:
                    data["expiration_date"] = valid_dates[-1] 
                else:
                    data["expiration_date"] = compute_expiry(data["filing_date"])
            except:
                data["expiration_date"] = compute_expiry(data["filing_date"])

            print(f"   📝 Enriched: {patent_id}")

        except Exception as e:
            print(f"   ⚠️ Failed to enrich {patent_id}: {e}")
        finally:
            await page.close()
            
        return data

# =========================
# MAIN
# =========================

async def main():
    # Base URL structure for assignee search
    BASE_URL = f"https://patents.google.com/?assignee={ASSIGNEE}"
    
    print(f"🚀 Starting scrape for assignee: '{ASSIGNEE}' (Max Pages: {MAX_PAGES})\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )

        # --- PHASE 1: SEARCH ---
        search_tasks = [scrape_search_page(context, BASE_URL, i) for i in range(1, MAX_PAGES + 1)]
        results_list = await asyncio.gather(*search_tasks)

        # Aggregate unique patent IDs
        all_patents = set().union(*results_list)
        print(f"\n🧾 Total unique patents found: {len(all_patents)}\n")

        if not all_patents:
            print("❌ No patents found. Exiting.")
            await browser.close()
            return

        # --- PHASE 2: ENRICH ---
        patent_list = sorted(list(all_patents))
        
        enrich_tasks = [enrich_patent(context, pid) for pid in patent_list]
        enriched_data = await asyncio.gather(*enrich_tasks)
        
        # Filter out None results
        enriched_data = [d for d in enriched_data if d]

        await browser.close()

    # --- SAVE ---
    if enriched_data:
        df = pd.DataFrame(enriched_data)
        df.to_csv(OUTPUT_FILE, index=False)
        print(f"\n{'='*50}")
        print(f"✅ SUCCESS: Saved {len(df)} records to {OUTPUT_FILE}")
        print(f"{'='*50}")
    else:
        print("❌ No data saved.")

if __name__ == "__main__":
    asyncio.run(main())
