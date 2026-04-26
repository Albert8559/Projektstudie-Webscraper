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

    try:
        await page.goto(url, timeout=30000)

        # wait for content
        await page.wait_for_load_state("networkidle")

        content = await page.content()

        if is_blocked(content):
            print("🚫 BLOCKED")
            return {"url": url, "patents": "", "count": 0, "status": "blocked"}

        patents = extract_patents(content)

        return {
            "url": url,
            "patents": "; ".join(patents),
            "count": len(patents),
            "status": "ok"
        }

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return {"url": url, "patents": "", "count": 0, "status": "error"}


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

        await browser.close()

    pd.DataFrame(results).to_csv(OUTPUT_FILE, index=False)
    print(f"\n✅ Done → {OUTPUT_FILE}")


# =========================
# ENTRY
# =========================

if __name__ == "__main__":
    asyncio.run(main())