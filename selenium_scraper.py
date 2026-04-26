import pandas as pd
import re
import os
import random
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Use undetected chromedriver (IMPORTANT)
import undetected_chromedriver as uc

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# =========================
# CONFIG
# =========================
INPUT_FILE = "urls.csv"
OUTPUT_FILE = "results_clean.csv"
DEBUG_DIR = "debug_output"

os.makedirs(DEBUG_DIR, exist_ok=True)


# =========================
# STRICT PATENT REGEX
# =========================
PATENT_REGEX = re.compile(
    r'\b(?:US|EP|WO|CN|JP|DE|GB|FR)\s?\d{4,}(?:[A-Z]\d?)?\b'
)


# =========================
# HELPERS
# =========================

def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def is_blocked_page(text: str) -> bool:
    signals = [
        "captcha",
        "puzzle",
        "verify you are human",
        "access denied",
        "bot detection",
        "cloudflare"
    ]
    text = text.lower()
    return any(s in text for s in signals)


def random_delay():
    time.sleep(random.uniform(2, 5))


def init_driver():
    options = uc.ChromeOptions()

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")

    driver = uc.Chrome(options=options)
    return driver


# =========================
# EXTRACTION LOGIC
# =========================

def extract_patents_general(driver):
    """Generic extraction using regex"""
    soup = BeautifulSoup(driver.page_source, "html.parser")
    text = soup.get_text(" ")

    matches = PATENT_REGEX.findall(text)

    # Clean + deduplicate
    clean = set(m.replace(" ", "") for m in matches)

    return list(clean)


def extract_patents_justia(driver):
    """Special handler for Justia"""
    patents = set()

    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/patent/']")

    for link in links:
        txt = link.text.strip()
        if PATENT_REGEX.search(txt):
            patents.add(txt.replace(" ", ""))

    return list(patents)


def extract_patents(driver, url):
    domain = urlparse(url).netloc

    if "justia.com" in domain:
        return extract_patents_justia(driver)

    return extract_patents_general(driver)


# =========================
# SCRAPER
# =========================

def scrape_url(driver, url):
    url = normalize_url(url)
    print(f"🔎 Scraping: {url}")

    try:
        driver.get(url)

        # smarter wait
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        random_delay()

        page_text = driver.page_source

        # detect block
        if is_blocked_page(page_text):
            print(f"🚫 BLOCKED: {url}")
            return {
                "url": url,
                "patents": "",
                "count": 0,
                "status": "blocked"
            }

        patents = extract_patents(driver, url)

        # debug if empty
        if not patents:
            print(f"⚠️ No patents found")

            filename = url.replace("https://", "").replace("http://", "").replace("/", "_")
            with open(f"{DEBUG_DIR}/{filename}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)

        return {
            "url": url,
            "patents": "; ".join(sorted(patents)),
            "count": len(patents),
            "status": "ok"
        }

    except Exception as e:
        print(f"❌ ERROR: {url} → {e}")
        return {
            "url": url,
            "patents": "",
            "count": 0,
            "status": "error"
        }


# =========================
# MAIN
# =========================

def main():
    df = pd.read_csv(INPUT_FILE)

    if "url" not in df.columns:
        raise ValueError("CSV must contain 'url' column")

    urls = df["url"].dropna().tolist()

    print(f"🚀 Processing {len(urls)} URLs...\n")

    driver = init_driver()
    results = []

    try:
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]")
            result = scrape_url(driver, url)
            results.append(result)

    finally:
        driver.quit()

    df_out = pd.DataFrame(results)
    df_out.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

    print(f"\n✅ DONE → {OUTPUT_FILE}")


# =========================
# ENTRY
# =========================

if __name__ == "__main__":
    main()