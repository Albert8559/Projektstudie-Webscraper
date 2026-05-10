import pandas as pd
import asyncio
import random
import re
import json
import torch
from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer

from playwright.async_api import async_playwright


# =========================
# CONFIG
# =========================
OUTPUT_FILE = "results_phi3.csv"

MAX_CONCURRENT_ENRICH = 2  # Keep low if you are on CPU
RETRIES = 3

SEARCH_QUERY = "%22Patent+Marking%22"
BASE_URL = "https://www.courtlistener.com"
NUM_PAGES = 11

# MODEL SETTINGS
# Using Microsoft Phi-3-mini-4k-instruct: 
# - Very small (4GB context window)
# - Fast on CPU
# - NO LOGIN REQUIRED
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
        print("📥 Loading Microsoft Phi-3 (First run only). Downloading ~2GB...")
        try:
            # Phi-3 requires trust_remote_code=True
            text_generator = pipeline(
                "text-generation",
                model=MODEL_ID,
                trust_remote_code=True, 
                device_map="auto", # Auto-detects GPU or CPU
                torch_dtype=torch.float16 if DEVICE == "cuda" else torch.float32
            )
            print("✅ Model loaded successfully.")
        except Exception as e:
            print(f"❌ Failed to load model: {e}")
            print("   Ensure you have 'torch' installed (pip install torch)")
    return text_generator


# =========================
# OPEN SOURCE ANALYSIS (PHI-3)
# =========================

async def analyze_with_phi3(text: str):
    """
    Runs Phi-3 to extract data.
    """
    # Truncate text to context window (4k tokens is plenty for us)
    truncated_text = " ".join(text.split()[:2000]) 

    # Phi-3 Chat Template
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

        # Apply Phi-3 chat template
        prompt = pipe.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        
        # Run in thread (Non-blocking)
        loop = asyncio.get_event_loop()
        outputs = await loop.run_in_executor(None, pipe, prompt, max_new_tokens=256, return_full_text=False)
        
        # Parse output
        raw_response = outputs[0]["generated_text"]
        
        # Clean up common LLM artifacts (Markdown ```json ... ```)
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if json_match:
            raw_response = json_match.group(0)
        
        data = json.loads(raw_response)
        
        return {
            "outcome": data.get("outcome"),
            "payment_found": data.get("payment_found", 0),
            "payment_amount": data.get("payment_amount")
        }
        
    except Exception as e:
        # JSON parsing errors are common in raw generation
        # print(f"   ⚠️ Phi-3 Error: {e}") # Optional: uncomment to debug
        return {
            "outcome": None,
            "payment_found": 0,
            "payment_amount": None
        }


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
            try:
                court_elem = page.locator("h4.case-court").first
                if await court_elem.count() > 0:
                    court_name = await court_elem.inner_text()
            except:
                pass
            
            # Regex Fallback
            if not court_name:
                try:
                    body_text = await page.locator("body").inner_text()
                    potential_courts = re.findall(r'(?:The\s+)?(?:United States\s+)?.*?\s+Court\s+(?:of\s+)?[\w\s,]+(?:District|Circuit|Appeals|Supreme)\s+[\w\s,]+', body_text)
                    if potential_courts:
                        court_name = potential_courts[0].strip()
                except:
                    pass
            
            case_data["court"] = court_name

            # -----------------------------
            # 2. TEXT EXTRACTION
            # -----------------------------
            selectors = ["article", "div.row.content", "div.col-sm-9.main.document", "div#opinion-content"]
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
                # -----------------------------
                # 3. LOCAL LLM CALL (PHI-3)
                # -----------------------------
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
    # Pre-load the model
    get_model_pipeline()

    urls = generate_urls(SEARCH_QUERY, NUM_PAGES)
    print(f"🚀 Processing {len(urls)} pages (Phi-3 Mode)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

        tasks = [with_retries(scrape_search_page, context, url) for url in urls]
        search_results = await asyncio.gather(*tasks)
        
        cases_to_enrich = []
        for res in search_results:
            if res: cases_to_enrich.extend(res)

        print(f"\n📋 Found {len(cases_to_enrich)} cases. Enriching with Phi-3...\n")

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
        print(f"Stats: Payments in {df_out['payment_found'].sum()} cases.")

if __name__ == "__main__":
    asyncio.run(main())