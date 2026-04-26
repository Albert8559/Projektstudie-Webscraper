import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import time
import random
import os

# --- Konfiguration ---
INPUT_FILE = 'urls.csv'
OUTPUT_FILE = 'results.csv'

# User-Agent simulieren
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# Regex für "Publication number: 12345678" (Global)
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
    """Analysiert Seiten mit intelligenter Listen/Tabellen-Suche und strengem Regex."""
    print(f"Analysiere: {base_url}")
    
    all_patents = set()
    virtual_link_found = False
    urls_to_visit = [base_url]
    max_depth = 4
    
    # STRICT REGEX (Innerhalb der Funktion, um Verwirrung zu vermeiden)
    # Erzwingt, dass die Nummer mit einer Ziffer beginnt und endet (keine trailing Bindestriche)
    STRICT_PATENT_REGEX = re.compile(
        r'\b(?:US|EP|DE|WO|CN|JP|GB|CA)\s?(?:Patent\s?)?(\d[\d\s\.,-]{5,}\d)(?:\s?[A-Z]\d)?\b', 
        re.IGNORECASE
    )
    
    def search_text_container(text):
        """Hilfsfunktion: Sucht in einem Textblock nach Patenten."""
        found = []
        # 1. Label Suche (z.B. "Publication number: 123")
        label_matches = PUBLICATION_LABEL_REGEX.findall(text)
        for m in label_matches:
            found.append(f"Pub-No: {m.strip()}")
        
        # 2. Standard Nummern Suche (Strict)
        matches = STRICT_PATENT_REGEX.findall(text)
        for m in matches:
            found.append(m.strip())
        return found

    for i, url in enumerate(urls_to_visit):
        if i >= max_depth:
            break
            
        try:
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            
            response = requests.get(url, headers=HEADERS, timeout=10)
            
            # CHECK: Ist es ein PDF?
            if 'application/pdf' in response.headers.get('content-type', '').lower():
                print(f"  -> PDF gefunden (Liste wahrscheinlich hier): {url}")
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
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # --- STRATEGIE: Zuerst Listen und Tabellen durchsuchen (Hohe Trefferwahrscheinlichkeit) ---
        
        # 1. Alle Unordered Lists (<ul>) prüfen
        for ul in soup.find_all('ul'):
            text = ul.get_text(separator=' ')
            patents = search_text_container(text)
            if patents:
                all_patents.update(patents)

        # 2. Alle Tabellen (<table>) prüfen
        for table in soup.find_all('table'):
            text = table.get_text(separator=' ')
            patents = search_text_container(text)
            if patents:
                all_patents.update(patents)
        
        # 3. Fallback: Wenn immer noch nichts gefunden, durchsuche Footer und ganzen Body
        if len(all_patents) == 0:
            footer = soup.find('footer')
            if footer:
                all_patents.update(search_text_container(footer.get_text()))
            else:
                all_patents.update(search_text_container(soup.get_text()))
        
        # --- DEBUG: Wenn 0 gefunden, speichere HTML zur manuellen Prüfung ---
        if len(all_patents) == 0 and i == 0:
             safe_name = base_url.replace("https://", "").replace("http://", "").replace("/", "_")[:30]
             # Erstelle Debug-Ordner wenn nicht vorhanden
             if not os.path.exists("debug_output"):
                 os.makedirs("debug_output")
             with open(f"debug_output/debug_{safe_name}.html", "w", encoding="utf-8") as f:
                 f.write(html)
             print(f"  -> 0 Patente. Debug-HTML in 'debug_output/' gespeichert.")

        # --- Link Sammlung (Nur auf der Startseite) ---
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
                        
        # Check für Virtual Marking Link (einfacher Text Check)
        if 'patent' in soup.get_text().lower():
            virtual_link_found = True

    # Ergebnis aufbereiten
    examples = ", ".join(list(all_patents)[:5])
    
    return {
        'URL': base_url,
        'Status': 'Success',
        'Patent_Numbers_Found': len(all_patents),
        'Patent_Examples': examples,
        'Virtual_Marking_Link': virtual_link_found,
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
        dummy_data = {'url': ['siemens.com', 'bosch.com', 'bmw.de', 'basf.com']}
        pd.DataFrame(dummy_data).to_csv(INPUT_FILE, index=False)
        print(f"Demo-Datei {INPUT_FILE} erstellt. Bitte anpassen und neu starten.")
        return

    results = []
    
    # 2. Durchlaufen der URLs
    print(f"Starte Scraper für {len(df_urls)} URLs...")
    for index, row in df_urls.iterrows():
        url = row['url']
        data = analyze_site(url)
        results.append(data)
        
        # Pause machen (Netiquette)
        sleep_time = random.uniform(1.0, 3.0)
        print(f"  -> Warte {sleep_time:.2f} Sekunden...")
        time.sleep(sleep_time)

    # 3. Speichern der Ergebnisse
    df_results = pd.DataFrame(results)
    df_results.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
    print(f"\nFertig! Ergebnisse gespeichert in {OUTPUT_FILE}")
    
    # Optional: Zusammenfassung drucken
    total_patents = df_results['Patent_Numbers_Found'].sum()
    print(f"Gesamt gefundene Patent-Referenzen: {total_patents}")

if __name__ == "__main__":
    main()