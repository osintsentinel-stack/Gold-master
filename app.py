import os
import json
import time
import threading
import logging
import requests
import math
import random
from datetime import datetime, timedelta

from flask import Flask, jsonify, Response
from flask_cors import CORS

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

PORT = int(os.environ.get("PORT", 10000))

# Persistent Global Memory (The 24-Hour State)
active_aircraft = {}
active_vessels = {}

# --- AIRCRAFT IDENTIFICATION MATRIX ---
def identify_airframe(callsign, icao):
    c = str(callsign).upper().strip()
    if c.startswith(("FORTE", "BLACKCAT")): return "RQ-4 Global Hawk (ISR Drone)"
    if c.startswith(("HOMER", "JAKE", "SNOOP", "OLIVE")): return "RC-135 Strategic Recon"
    if c.startswith(("PUMA", "GORGON", "WARWAR")): return "MQ-9 Reaper (Armed UAV)"
    if c.startswith("RCH"): return "C-17 Globemaster III (Heavy Lift)"
    if c.startswith(("LAGR", "QID", "CLEAN", "HOBO")): return "KC-135 Stratotanker"
    if c.startswith(("VIPER", "VENOM", "BART")): return "Tactical Fighter / Interceptor"
    if c.startswith("CNV"): return "US Navy Logistics"
    return "Military/Gov Asset"

# ============================================================
# DISTRIBUTED SENSOR SCRAPERS
# ============================================================

def airspace_monitor():
    """Polls ADSB.lol (Unfiltered open-source transponders)"""
    # Targeting Eastern Europe & Levant bounding box to optimize payload
    url = "https://api.adsb.lol/v2/lat/35/lon/35/dist/1500"
    
    while True:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                now = datetime.utcnow()
                
                if data and data.get("ac"):
                    for ac in data["ac"]:
                        lat, lon = ac.get("lat"), ac.get("lon")
                        if not lat or not lon: continue
                        
                        icao = ac.get("icao", "UNKNOWN")
                        callsign = ac.get("flight", "HIDDEN").strip()
                        is_mil = ac.get("mil", False) or callsign.startswith(("FORTE", "RCH", "JAKE"))
                        
                        # We only track Military/Gov for the tactical picture to save memory
                        if not is_mil and callsign == "HIDDEN": continue
                        
                        speed, alt = ac.get("gs", 0), ac.get("alt_baro", 0)
                        heading = ac.get("track", 0)
                        
                        if icao in active_aircraft:
                            # Update existing track
                            active_aircraft[icao].update({
                                "lat": lat, "lng": lon, "speed": speed, "alt": alt,
                                "heading": heading, "last_seen": now
                            })
                            # Append to breadcrumb path
                            if active_aircraft[icao]["path"][-1] != [lon, lat]:
                                active_aircraft[icao]["path"].append([lon, lat])
                            if len(active_aircraft[icao]["path"]) > 50: 
                                active_aircraft[icao]["path"].pop(0)
                        else:
                            # Register new track
                            active_aircraft[icao] = {
                                "id": icao, "callsign": callsign, 
                                "airframe": identify_airframe(callsign, icao) if is_mil else "Commercial",
                                "type": "MILITARY/GOV" if is_mil else "COMMERCIAL",
                                "color": "#ff3333" if is_mil else "#00ff41",
                                "lat": lat, "lng": lon, "speed": speed, "alt": alt,
                                "heading": heading, "path": [[lon, lat]], "last_seen": now
                            }
                            
                # Prune aircraft not seen in 15 minutes (Memory Management)
                limit = now - timedelta(minutes=15)
                keys_to_del = [k for k, v in active_aircraft.items() if v["last_seen"] < limit]
                for k in keys_to_del: del active_aircraft[k]
                
        except Exception as e:
            logger.error(f"Airspace API Error: {e}")
            
        time.sleep(20)

def maritime_monitor():
    """Polls public AIS data with rotated User-Agents to bypass Cloudflare"""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ]
    url = "https://www.myshiptracking.com/requests/vesselsonmap.php?type=json&minlat=20&maxlat=50&minlon=20&maxlon=50"
    
    while True:
        try:
            headers = {'User-Agent': random.choice(user_agents), 'Accept': 'application/json'}
            res = requests.get(url, timeout=10, headers=headers)
            
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    now = datetime.utcnow()
                    for v in data:
                        try:
                            mmsi, lat, lon, speed = str(v[0]), float(v[1]), float(v[2]), float(v[3])
                            heading = float(v[4]) if len(v) > 4 else 0
                            name = v[6] if len(v) > 6 else f"VESSEL-{mmsi[-4:]}"
                            v_code = str(v[7]) if len(v) > 7 else "0"
                            
                            is_combatant = v_code in ["35", "36", "37"]
                            
                            active_vessels[mmsi] = {
                                "mmsi": mmsi, "name": name, 
                                "type": "NATO_NAVY" if is_combatant else "MERCHANT",
                                "color": "#ff3333" if is_combatant else "#3399ff",
                                "lat": lat, "lng": lon, "speed": speed, "heading": heading,
                                "last_seen": now
                            }
                        except (IndexError, ValueError):
                            continue
                            
                    # Prune stale vessels (30 mins)
                    limit = now - timedelta(minutes=30)
                    keys_to_del = [k for k, v in active_vessels.items() if v["last_seen"] < limit]
                    for k in keys_to_del: del active_vessels[k]
                    
        except Exception as e:
            logger.error(f"Maritime API Error: {e}")
            
        time.sleep(25)

# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/airspace")
def get_airspace():
    features = [{"type":"Feature","geometry":{"type":"Point","coordinates":[a['lng'], a['lat']]},"properties":a} for a in active_aircraft.values()]
    return jsonify({"type":"FeatureCollection","features":features})

@app.route("/vessels")
def get_vessels():
    features = [{"type":"Feature","geometry":{"type":"Point","coordinates":[v['lng'], v['lat']]},"properties":v} for v in active_vessels.values()]
    return jsonify({"type":"FeatureCollection","features":features})

@app.route("/stream")
def stream_alerts():
    def generate():
        while True:
            yield ": keepalive\n\n"
            time.sleep(15)
    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    threading.Thread(target=airspace_monitor, daemon=True).start()
    threading.Thread(target=maritime_monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
