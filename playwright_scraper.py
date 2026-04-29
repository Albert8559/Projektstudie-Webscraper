import pandas as pd
import re
import asyncio
from urllib.parse import urlparse

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
INPUT_FILE = "urls.csv"
OUTPUT_FILE = "results_playwright.csv"
# Safety limit to stop infinite loops if a site has thousands of pages
MAX_PAGES = 20 


# =========================
# STRICT PATENT REGEX
# =========================
PATENT_REGEX = re.compile(
    r'\b(?:US|EP|WO|CN|JP|DE|GB|FR)\d{4,}(?:[A-Z]\d?)?\b'
)


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


# =========================
# EXTRACTION
# =========================

def extract_patents(text):
    matches = PATENT_REGEX.findall(text)
    return list(set(matches))


async def scrape_url(page, url):
    url = normalize_url(url)
    print(f"🔎 {url}")
    
    all_patents = set()
    
    try:
        await page.goto(url, timeout=30000)
        
        for page_num in range(1, MAX_PAGES + 1):
            print(f"   -> Scanning page {page_num}...")
            await page.wait_for_load_state("networkidle")
            
            content = await page.content()

            if is_blocked(content):
                print("🚫 BLOCKED")
                break

            patents = extract_patents(content)
            all_patents.update(patents)
            
            # --- ENHANCED PAGINATION LOGIC FOR POLYMER/GOOGLE SITES ---
            
            next_selectors = [
                # 1. GOOGLE / POLYMER SPECIFIC (Based on your snippet)
                # These look for the accessibility label which screen readers use
                'paper-icon-button[aria-label*="next" i]', 
                'iron-icon[aria-label*="next" i]',
                'button[aria-label*="next" i]',
                'a[aria-label*="next" i]',

                # 2. SPECIFIC SVG PATH (The "Arrow" shape)
                # This is a failsafe: it looks for the actual drawing code of a right-arrow
                'svg path[d*="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"]',

                # 3. STANDARD TEXT (Fallback for non-Google sites)
                'a:has-text("Next")',
                'button:has-text("Next")',
                'a:has-text(">")',
                'button:has-text(">")',
                
                # 4. CSS CLASS FALLBACKS
                '.pagination-next',
                '#next'
            ]
            
            next_button = None
            
            # Try to find a visible, enabled button from the list
            for selector in next_selectors:
                try:
                    # We use .first to ensure we only grab one button
                    element = page.locator(selector).first
                    
                    # Check if it exists, is visible, and is clickable
                    if await element.is_visible() and await element.is_enabled():
                        next_button = element
                        print(f"   -> Found next button using selector: {selector}")
                        break
                except:
                    continue
            
            if next_button:
                print(f"   -> Clicking to go to page {page_num + 1}")
                
                # Scroll to the button to ensure it's clickable (sometimes needed on long pages)
                await next_button.scroll_into_view_if_needed()
                
                await next_button.click()
                # Wait a moment for the click to register and the new page request to start
                await page.wait_for_timeout(1500) 
            else:
                print(f"   -> No 'Next' button found. Finished scraping.")
                break

        return {
            "url": url,
            "patents": "; ".join(sorted(list(all_patents))),
            "count": len(all_patents),
            "status": "ok"
        }

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return {
            "url": url, 
            "patents": "; ".join(sorted(list(all_patents))),
            "count": len(all_patents), 
            "status": "error"
        }

# =========================
# MAIN
# =========================

async def main():
    df = pd.read_csv(INPUT_FILE)
    urls = df["url"].dropna().tolist()

    print(f"🚀 Processing {len(urls)} URLs...\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )

        page = await context.new_page()

        results = []

        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]")
            result = await scrape_url(page, url)
            results.append(result)
            print(f"   -> Total patents found: {result['count']}\n")

        await browser.close()

    pd.DataFrame(results).to_csv(OUTPUT_FILE, index=False)
    print(f"\n✅ Done → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())