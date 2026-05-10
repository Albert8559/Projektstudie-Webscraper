import pandas as pd
import asyncio
import random
import re
import json
import torch
from transformers import pipeline

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "results_debug.csv"
DEBUG_SAVE_HTML = True  # Set to False if you don't want files created
DEBUG_FILE = "debug_failures.html"

MAX_CONCURRENT_ENRICH = 2
RETRIES = 3

SEARCH_QUERY = "%22Patent+Marking%22"
BASE_URL = "https://www.courtlistener.com"
NUM_PAGES = 11

# MODEL SETTINGS (Using Phi-3 as before)
MODEL_ID = "microsoft/Phi-3-mini-4k-instruct"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

print(f"🤖 Using Model: {MODEL_ID}")
print(f"🤖 Using Device: {DEVICE}")


# =========================
# GLOBAL MODEL INIT
# =========================

text_generator = None

def get_model_pipeline():
    global text_generator
    if text_generator is None:
        print("📥 Loading model...")
        try:
            text_generator = pipeline(
                "text-generation",
                model=MODEL_ID,
                trust_remote_code=True, 
                device_map="auto",
                torch_dtype=torch.float16 if DEVICE == "cuda" else torch.float32
            )
            print("✅ Model loaded.")
        except Exception as e:
            print(f"❌ Model load failed: {e}")
    return text_generator


# =========================
# ANALYSIS
# =========================

async def analyze_with_phi3(text: str):
    truncated_text = " ".join(text.split()[:2000]) 
    messages = [
        {"role": "system", "content": "You are a precise legal data extractor. Output ONLY valid JSON."},
        {"role": "user", "content": f"""
Extract the following from this text. Return ONLY JSON.
1. "outcome": 1 if Plaintiff Won, 0 if Defendant Won, null if unclear.
2. "payment_found": 1 if damages/costs/fees mentioned, 0 otherwise.
3. "payment_amount": The amount (e.g., "$15.7 million"), or null.

Text:
{truncated_text}
"""}
    ]
    try:
        pipe = get_model_pipeline()
        if pipe is None:
            return {"outcome": None, "payment_found": 0, "payment_amount": None}

        prompt = pipe.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        loop = asyncio.get_event_loop()
        outputs = await loop.run_in_executor(None, pipe, prompt, max_new_tokens=256, return_full_text=False)
        
        raw_response = outputs[0]["generated_text"]
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if json_match:
            raw_response = json_match.group(0)
        
        data = json.loads(raw_response)
        return {
            "outcome": data.get("outcome"),
            "payment_found": data.get("payment_found", 0),
            "payment_amount": data.get("payment_amount")
        }
    except Exception:
        return {"outcome": None, "payment_found": 0, "payment_amount": None}


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
enrich_sem = asyncio.Semaphore(MAX_CONCURRENT_ENRICH)


# =========================
# SCRAPING
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
                    else: continue 

                    time_elem = article.locator('div.bottom div.inline-block time').first
                    data["date_filed"] = await time_elem.get_attribute("datetime") if await time_elem.count() > 0 else None

                    docket_elem = article.locator('span.meta-data-value.select-all').first
                    data["docket_number"] = await docket_elem.inner_text() if await docket_elem.count() > 0 else None

                    if data.get("case_name"):
                        results_on_page.append(data)
                except Exception: continue
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
            # Wait for title to change to ensure page load started
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except:
                pass

            # -----------------------------
            # 0. CAPTCHA CHECK
            # -----------------------------
            page_title = await page.title()
            if "Just a moment" in page_title or "Attention Required" in page_title or "Access Denied" in page_title:
                print(f"   ❌ BLOCKED/CAPTCHA detected on {url}")
                # Save HTML for debugging
                if DEBUG_SAVE_HTML:
                    with open(DEBUG_FILE, "a", encoding="utf-8") as f:
                        f.write(f"\n\n<!-- BLOCKED URL: {url} -->\n")
                        f.write(await page.content())
                return case_data

            # -----------------------------
            # 1. ROBUST COURT EXTRACTION
            # -----------------------------
            court_name = None
            
            # Method A: Direct Class
            try:
                court_elem = page.locator("h4.case-court").first
                if await court_elem.count() > 0:
                    court_name = await court_elem.inner_text()
            except:
                pass
            
            # Method B: Regex Fallback on Body
            if not court_name:
                try:
                    # We grab the whole body text to find the court name
                    body_text = await page.locator("body").inner_text()
                    # Regex looking for "Court of Appeals..." or "District Court..."
                    potential_courts = re.findall(r'(?:United States\s+)?(?:Court of Appeals for the\s+[\w\s]+|District Court,?\s+(?:[A-Z]\.?\s?)+|District of\s+[\w\s]+|Circuit Court)', body_text)
                    if potential_courts:
                        court_name = potential_courts[0].strip()
                except:
                    pass
            
            case_data["court"] = court_name

            # -----------------------------
            # 2. TEXT EXTRACTION (AGGRESSIVE)
            # -----------------------------
            
            # Priority 1: Standard Opinion Containers
            selectors = [
                "article",
                "div.row.content", 
                "div.col-sm-9.main.document",
                "div#opinion-content"
            ]
            
            full_text = ""
            found_selector = None

            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() > 0:
                        text = await locator.inner_text()
                        if len(text) > 200:
                            full_text = text
                            found_selector = selector
                            break
                except:
                    continue

            # Priority 2: THE "NUCLEAR" OPTION (If standard selectors failed)
            if not full_text:
                print(f"   ⚠️ Standard selectors failed. Attempting Full Body Text extraction.")
                try:
                    # We grab everything in the body. This might include headers, footers, menus, etc.
                    # We clean it up by looking for the largest block of text if possible, but here we just take raw body text.
                    body_text = await page.locator("body").inner_text()
                    
                    # Simple heuristic: Remove very short lines (likely nav items)
                    lines = body_text.split('\n')
                    long_lines = [line for line in lines if len(line) > 50]
                    full_text = "\n".join(long_lines)
                    
                    if len(full_text) < 100:
                        full_text = "" # Still nothing useful
                        # SAVE DEBUG HTML
                        if DEBUG_SAVE_HTML:
                            print(f"   🐞 Saving HTML to {DEBUG_FILE} for manual inspection.")
                            with open(DEBUG_FILE, "a", encoding="utf-8") as f:
                                f.write(f"\n\n<!-- FAILED URL: {url} -->\n")
                                f.write(await page.content())
                except Exception as e:
                    print(f"   ❌ Failed to extract body text: {e}")

            # -----------------------------
            # 3. ANALYSIS
            # -----------------------------
            if not full_text:
                print(f"   ⚠️ No text content found (even in body).")
                case_data["outcome"] = None
                case_data["payment_found"] = 0
                case_data["payment_amount"] = None
            else:
                analysis = await analyze_with_phi3(full_text)
                case_data["outcome"] = analysis["outcome"]
                case_data["payment_found"] = analysis["payment_found"]
                case_data["payment_amount"] = analysis["payment_amount"]

        except Exception as e:
            print(f"⚠️ Critical error on {url}: {e}")
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
    get_model_pipeline()
    urls = generate_urls(SEARCH_QUERY, NUM_PAGES)
    print(f"🚀 Processing {len(urls)} pages (Debug Mode)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

        tasks = [with_retries(scrape_search_page, context, url) for url in urls]
        search_results = await asyncio.gather(*tasks)
        
        cases_to_enrich = []
        for res in search_results:
            if res: cases_to_enrich.extend(res)

        print(f"\n📋 Found {len(cases_to_enrich)} cases. Enriching...\n")

        enrich_tasks = [with_retries(enrich_case_details, context, case) for case in cases_to_enrich]
        final_data = await asyncio.gather(*enrich_tasks)

        await browser.close()

    if final_data:
        df_out = pd.DataFrame(final_data)
        cols = ["case_name", "docket_number", "date_filed", "url", "court", "payment_found", "payment_amount", "outcome"]
        for col in cols: 
            if col not in df_out.columns: df_out[col] = None
            
        df_out = df_out[cols]
        df_out.to_csv(OUTPUT_FILE, index=False, encoding='utf-8')
        print(f"\n✅ Saved to {OUTPUT_FILE}")
        if DEBUG_SAVE_HTML:
            print(f"🐞 Check '{DEBUG_FILE}' for any HTML that failed extraction.")

if __name__ == "__main__":
    asyncio.run(main())