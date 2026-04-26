import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import time
import random

# --- Konfiguration ---
INPUT_FILE = 'urls.csv'
OUTPUT_FILE = 'results.csv'

# User-Agent simulieren
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# Reguläre Ausdrücke (Regex) für Patente
PATENT_REGEX = re.compile(
    r'\b(?:US|EP|DE|WO|CN|JP|GB|CA)\s?(?:Patent\s?)?[\d,\-\s]{4,}(?:\s?[A-Z]\d)?\b', 
    re.IGNORECASE
)
# Erkennt "Publication number: 12345678"
PUBLICATION_LABEL_REGEX = re.compile(r'(?:Publication|Patent)\s?number:\s?([\d\-\s,]+)', re.IGNORECASE)

def get_absolute_url(base, link):
    """Converts relative URLs (e.g. /patents) to absolute URLs."""
    if link.startswith('http'):
        return link
    elif link.startswith('/'):
        return base.rstrip('/') + link
    else:
        return base.rstrip('/') + '/' + link

def analyze_site(base_url):
    """Analysiert Hauptseite UND folgt Patent-Links."""
    print(f"Analysiere: {base_url}")
    
    all_patents = set()
    virtual_link_found = False
    urls_to_visit = [base_url]
    max_depth = 4 
    
    for i, url in enumerate(urls_to_visit):
        if i >= max_depth:
            break
            
        try:
            # Sicherstellen, dass URL http/https hat
            current_url = url
            if not current_url.startswith(('http://', 'https://')):
                current_url = 'https://' + current_url
            
            response = requests.get(current_url, headers=HEADERS, timeout=10)
            
            # CHECK: Ist es ein PDF?
            content_type = response.headers.get('content-type', '').lower()
            if 'application/pdf' in content_type:
                print(f"  -> PDF gefunden: {current_url}")
                return {
                    'URL': base_url,
                    'Status': 'Success',
                    'Patent_Numbers_Found': 1, 
                    'Patent_Examples': 'See PDF File',
                    'Virtual_Marking_Link': True,
                    'Pages_Visited': len(urls_to_visit)
                }
            
            if response.status_code != 200:
                continue
                
            html = response.text
            
        except Exception as e:
            print(f"Verbindungsfehler bei {url}: {e}")
            continue
        
        # --- HTML Parsing ---
        soup = BeautifulSoup(html, 'html.parser')
        
        # 1. Standard Patent Suche (Text + Quellcode)
        visible_text = soup.get_text()
        standard_matches = PATENT_REGEX.findall(visible_text) + PATENT_REGEX.findall(html)
        for m in standard_matches:
            all_patents.add(m.strip())

        # 2. Label Suche (z.B. "Publication number: ...")
        label_matches = PUBLICATION_LABEL_REGEX.findall(html)
        for number in label_matches:
            clean_number = " ".join(number.split())
            all_patents.add(f"Pub-No: {clean_number}")

        # 3. Check für Virtual Marking Link (Text enthält "patent" im Link)
        for a_tag in soup.find_all('a', href=True):
            if 'patent' in a_tag.get_text().lower():
                virtual_link_found = True

        # 4. Links finden für weitere Seiten (Nur auf der ersten Seite, um Endlosschleifen zu vermeiden)
        if i == 0: 
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href'].lower()
                link_text = a_tag.get_text().lower()
                
                # Keywords für Legal/IP Seiten
                keywords = ['patent', 'intellectual', 'ip', 'legal', 'impressum', 'rechtliches']
                if any(k in href or k in link_text for k in keywords):
                    full_url = get_absolute_url(base_url, a_tag['href'])
                    if full_url not in urls_to_visit:
                        urls_to_visit.append(full_url)
                        # print(f"  -> Following link: {full_url}") # Optional: Debug Output

    # Ergebnis zurückgeben
    examples = ", ".join(list(all_patents)[:5])
    
    return {
        'URL': base_url,
        'Status': 'Success',
        'Patent_Numbers_Found': len(all_patents),
        'Patent_Examples': examples,
        'Virtual_Marking_Link': virtual_link_found, # HIER: Variable statt False
        'Pages_Visited': len(urls_to_visit)
    }

def main():
    # 1. URLs laden
    try:
        df_urls = pd.read_csv(INPUT_FILE)
        if 'url' not in df_urls.columns:
            print("Fehler: CSV muss eine Spalte 'url' haben.")
            return
    except FileNotFoundError:
        print(f"Datei {INPUT_FILE} nicht gefunden. Erstelle Demo-Datei...")
        dummy_data = {'url': ['siemens.com', 'bosch.com', 'bmw.de', 'festo.com']}
        pd.DataFrame(dummy_data).to_csv(INPUT_FILE, index=False)
        print(f"Demo-Datei {INPUT_FILE} erstellt. Bitte anpassen und neu starten.")
        return

    results = []
    
    # 2. Durchlaufen der URLs
    for index, row in df_urls.iterrows():
        url = row['url']
        data = analyze_site(url)
        results.append(data)
        
        # Pause machen (Netiquette)
        time.sleep(random.uniform(1.0, 3.0))

    # 3. Speichern der Ergebnisse
    df_results = pd.DataFrame(results)
    df_results.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
    print(f"\nFertig! Ergebnisse gespeichert in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()