import os
import requests
import json
import time
from datetime import datetime, timedelta
import gspread

# --- CONFIGURAZIONE ---
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
SORARE_API_KEY = os.environ.get("SORARE_API_KEY")
GSPREAD_CREDENTIALS_JSON = os.environ.get("GSPREAD_CREDENTIALS")
API_URL = "https://api.sorare.com/graphql"
STATE_FILE = "sent_notifications.json"
NOTIFICATION_COOLDOWN_HOURS = 6

# ID e Nome del tuo Foglio Google
SPREADSHEET_ID = "1PTNR8xoBGzTCWCXCrr9rOnNgGcIrgFpCsaDwwvEYa3w"
WORKSHEET_NAME = "ALLERT"

# Configurazione Tolleranza Livello Alto
HIGH_LEVEL_THRESHOLD = 5
HIGH_LEVEL_PRICE_TOLERANCE_EUR = 0.15

# --- QUERY GRAPHQL (invariate) ---
LOWEST_PRICE_QUERY = """
    query GetLowestPrice($playerSlug: String!, $rarity: Rarity!, $inSeason: Boolean) {
      football {
        player(slug: $playerSlug) {
          displayName
          lowestPriceAnyCard(rarity: $rarity, inSeason: $inSeason) {
            slug
            grade
            liveSingleSaleOffer {
              receiverSide {
                amounts { eurCents, wei }
              }
            }
          }
        }
      }
    }
"""
UTILITY_QUERY = """ query UtilityQuery { utility { ethToEurRate } } """

# --- FUNZIONI HELPER (invariate) ---
def get_sorare_eth_rate():
    try:
        headers = {"APIKEY": SORARE_API_KEY, "Content-Type": "application/json"}
        payload = {"query": UTILITY_QUERY}
        response = requests.post(API_URL, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()
        rate_str = data.get("data", {}).get("utility", {}).get("ethToEurRate")
        if rate_str: return float(rate_str)
    except Exception: pass
    return None

def get_coingecko_eth_rate():
    try:
        response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=eur", timeout=5)
        response.raise_for_status()
        return response.json()["ethereum"]["eur"]
    except Exception: pass
    return None

def get_best_eth_rate():
    rate = get_sorare_eth_rate()
    if rate: print("Tasso di cambio ottenuto da Sorare."); return rate
    rate = get_coingecko_eth_rate()
    if rate: print("Tasso di cambio ottenuto da CoinGecko."); return rate
    print("ERRORE CRITICO: Impossibile ottenere il tasso di cambio da qualsiasi fonte.")
    return None

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL: return
    payload = {"content": message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        print("Notifica Discord inviata con successo!")
    except requests.exceptions.RequestException as e: print(f"ERRORE invio notifica Discord: {e}")

def load_sent_notifications():
    try:
        with open(STATE_FILE, "r") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_sent_notifications(state_data):
    with open(STATE_FILE, "w") as f: json.dump(state_data, f, indent=2)

# --- LOGICA PRINCIPALE DI CONTROLLO ---
def check_single_player_price(target, eth_rate, sent_notifications):
    player_slug = target.get('slug')
    price_str = target.get('price')

    if not player_slug or not price_str: return False
    
    try:
        target_price = float(str(price_str).replace(',', '.'))
    except (ValueError, TypeError):
        print(f"Attenzione: prezzo non valido per {player_slug}: '{price_str}'. Riga saltata.")
        return False

    rarity = target.get('rarity')
    season_preference = target.get('season', 'classic')
    season_text = "In Season" if season_preference == "in_season" else "Classic (Any Season)"
    
    print(f"\n--- Controllando {player_slug} ({rarity}, {season_text}) con obiettivo <= {target_price}€ ---")
    
    headers = {"APIKEY": SORARE_API_KEY, "Content-Type": "application/json"}
    variables = {"playerSlug": player_slug, "rarity": rarity}
    if season_preference == 'in_season': variables['inSeason'] = True
    payload = {"query": LOWEST_PRICE_QUERY, "variables": variables}

    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            print(f"Errore GraphQL per {player_slug}: {data['errors']}")
            return False

        lowest_card_info = data.get("data", {}).get("football", {}).get("player", {}).get("lowestPriceAnyCard")
        if not lowest_card_info:
            print(f"Nessuna carta '{rarity}' ({season_text}) per {player_slug} trovata sul mercato.")
            return False

        unique_card_slug = lowest_card_info.get("slug")
        if not unique_card_slug:
            print("Attenzione: lo slug unico della carta non è stato trovato nella API.")
            return False
            
        alert_key = unique_card_slug

        last_notified_str = sent_notifications.get(alert_key)
        if last_notified_str:
            last_notified_time = datetime.fromisoformat(last_notified_str)
            if datetime.utcnow() < last_notified_time + timedelta(hours=NOTIFICATION_COOLDOWN_HOURS):
                print(f"--- Saltando la carta {unique_card_slug}: notifica già inviata di recente. ---")
                return False
        
        player_name = data["data"]["football"]["player"].get("displayName", player_slug)
        amounts = lowest_card_info.get("liveSingleSaleOffer", {}).get("receiverSide", {}).get("amounts")
        card_level = lowest_card_info.get("grade", 0)

        current_price = 0
        if amounts and amounts.get("eurCents"): current_price = amounts["eurCents"] / 100
        elif amounts and amounts.get("wei") and eth_rate:
            current_price = (float(amounts["wei"]) / 1e18) * eth_rate
        
        if current_price > 0:
            print(f"Prezzo più basso ({unique_card_slug}): {current_price:.2f}€, Livello {card_level}")
            
            should_notify = False
            notification_reason = ""

            if current_price <= target_price:
                should_notify = True
                notification_reason = "Prezzo Obiettivo Raggiunto"
            elif card_level >= HIGH_LEVEL_THRESHOLD and current_price <= (target_price + HIGH_LEVEL_PRICE_TOLERANCE_EUR):
                should_notify = True
                notification_reason = f"Livello Alto (>= {HIGH_LEVEL_THRESHOLD})"
            
            if should_notify:
                print(f"!!! CONDIZIONE SODDISFATTA ({notification_reason}) PER {unique_card_slug}!!! Invio notifica...")
                market_url = f"https://sorare.com/cards/{unique_card_slug}"
                message = (
                    f"🔥 **Allerta Prezzo Sorare!** 🔥\n\n"
                    f"Trovata carta per **{player_name}** ({rarity.capitalize()}) sotto il tuo prezzo obiettivo!\n\n"
                    f"**Carta Specifica:** `{unique_card_slug}`\n"
                    f"**Livello Carta:** `{card_level}`\n"
                    f"**Motivo Allerta:** {notification_reason}\n"
                    f"**Tipo Carta:** {season_text}\n"
                    f"📉 **Prezzo Trovato: {current_price:.2f}€**\n"
                    f"🎯 **Prezzo Obiettivo: {target_price:.2f}€** (+{HIGH_LEVEL_PRICE_TOLERANCE_EUR}€ di tolleranza per livello >= {HIGH_LEVEL_THRESHOLD})\n\n"
                    f"➡️ **LINK DIRETTO ALLA CARTA:** {market_url}"
                )
                send_discord_notification(message)
                sent_notifications[alert_key] = datetime.utcnow().isoformat()
                return True
            else:
                print("Prezzo superiore all'obiettivo (e alla tolleranza).")
        else:
            print(f"Nessun prezzo valido trovato per {unique_card_slug}.")
            
    except Exception as e:
        print(f"Errore imprevisto durante il controllo di {player_slug}: {e}")

    return False

# --- FUNZIONE DI AVVIO ---
def main():
    if not all([SORARE_API_KEY, DISCORD_WEBHOOK_URL, GSPREAD_CREDENTIALS_JSON]):
        print("ERRORE: Mancano uno o più segreti.")
        return
    
    try:
        print("Autenticazione a Google Sheets...")
        credentials = json.loads(GSPREAD_CREDENTIALS_JSON)
        gc = gspread.service_account_from_dict(credentials)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        
        # --- [MODIFICA CHIAVE] LETTURA DEI VALORI GREZZI ---
        # Leggiamo tutti i valori come stringhe per evitare conversioni automatiche
        all_values = worksheet.get_all_values()
        
        # La prima riga è l'intestazione
        headers = all_values[0]
        # Le righe successive sono i dati
        data_rows = all_values[1:]
        
        # Ricostruiamo la lista di dizionari, come faceva get_all_records()
        targets = []
        for row in data_rows:
            # zip abbina ogni header con il suo valore nella riga
            targets.append(dict(zip(headers, row)))
        # ----------------------------------------------------
            
        print(f"Trovati {len(targets)} giocatori da monitorare dal Foglio Google '{WORKSHEET_NAME}'.")
    except Exception as e:
        print(f"ERRORE CRITICO durante l'accesso a Google Sheets: {e}")
        return
    
    eth_to_eur_rate = get_best_eth_rate()
    if eth_to_eur_rate:
        print(f"Utilizzando il tasso di cambio: 1 ETH = {eth_to_eur_rate:.2f}€")

    sent_notifications = load_sent_notifications()
    state_was_modified = False
    
    for target in targets:
        if target.get('slug'):
            if check_single_player_price(target, eth_to_eur_rate, sent_notifications):
                state_was_modified = True
            time.sleep(1) 
    
    if state_was_modified:
        print("\nSalvataggio dello stato delle notifiche aggiornato...")
        save_sent_notifications(sent_notifications)
    
    print("\n--- Controllo completato. ---")

if __name__ == "__main__":
    main()
