#!/usr/bin/env python3
"""
Adaptiv prognosmodul för Sol och Huslast (48 timmar med 15-minutersupplösning = 192 kvartar).
Hämtar data från Home Assistant (Solcast, NIBE/BT1, SMHI, Solis huslast) via REST API,
interpolerar till kvartar, applicerar adaptiv EWMA/PID-korrigering och publicerar till HA.
"""

import os
import json
import time
import datetime
import hmac
import hashlib
import base64
import paramiko

# Konstanter för HA-åtkomst
HA_HOST = "10.12.22.12"
HA_USER = "root"
SSH_KEY = "/workspace/id_ed25519"
REFRESH_TOKEN_ID = "2e524fc4f7254527b734d719720a6230"
JWT_KEY = "4bf3f2bb0d22f9220eb4ca4a90263b81b071c05b86a50e27104dcfe392ee49b71840a81f8bedab36d28450d428e380021d34da8c9a79f9570bc5f7f146b77826"
PERSISTENCE_FILE = "/workspace/projects/Batteristyrning - effektavgift/src/forecast_state.json"

def generate_ha_token():
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": REFRESH_TOKEN_ID,
        "iat": now,
        "exp": now + 3600 * 24 * 30  # 30 dagar
    }
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    signing_input = b64(json.dumps(header, separators=(",", ":")).encode()) + "." + b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(JWT_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64(sig)

class HAForecastClient:
    def __init__(self):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(HA_HOST, username=HA_USER, key_filename=SSH_KEY)
        self.token = generate_ha_token()

    def get_state(self, entity_id):
        cmd = f'curl -s http://172.30.32.1:8123/api/states/{entity_id} -H "Authorization: Bearer {self.token}"'
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        res = stdout.read().decode()
        try:
            return json.loads(res)
        except Exception:
            return None

    def post_state(self, entity_id, state, attributes=None):
        payload = {"state": str(state), "attributes": attributes or {}}
        cmd = f'''curl -s -X POST http://172.30.32.1:8123/api/states/{entity_id} \\
          -H "Authorization: Bearer {self.token}" \\
          -H "Content-Type: application/json" \\
          -d '{json.dumps(payload)}' '''
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        return stdout.read().decode()

    def get_weather_forecast(self):
        cmd = f'''curl -s -X POST "http://172.30.32.1:8123/api/services/weather/get_forecasts?return_response=true" \\
          -H "Authorization: Bearer {self.token}" \\
          -H "Content-Type: application/json" \\
          -d '{{"entity_id": "weather.smhi_home", "type": "hourly"}}' '''
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        res = json.loads(stdout.read().decode())
        return res.get("service_response", {}).get("weather.smhi_home", {}).get("forecast", [])

    def read_solcast_json(self):
        sftp = self.ssh.open_sftp()
        with sftp.open("/homeassistant/solcast_solar/solcast.json") as f:
            data = json.load(f)
        site = list(data["siteinfo"].keys())[0]
        return data["siteinfo"][site]["forecasts"]

    def close(self):
        self.ssh.close()

def load_state():
    if os.path.exists(PERSISTENCE_FILE):
        try:
            with open(PERSISTENCE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "integral_bias_kw": 0.0,
        "last_predicted_kw": 1.5,
        "last_updated_epoch": 0,
        "solar_scale_factor": 1.0
    }

def save_state(state):
    with open(PERSISTENCE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def generate_15m_timestamps(start_dt, steps=192):
    # Runda start_dt till närmaste föregående kvart
    minute = (start_dt.minute // 15) * 15
    base = start_dt.replace(minute=minute, second=0, microsecond=0)
    return [base + datetime.timedelta(minutes=15 * i) for i in range(steps)]

def base_load_kw(dt, temp_c):
    """
    Parametrisk baslast för hushållet:
    - Temperaturkänslighet: cirka 70 W per grad C under 17 C (värmepump).
    - Tidsprofil: morgonpeak (07-09), kvällspeak (17-21), låglast natt (23-06).
    """
    hour = dt.hour + dt.minute / 60.0
    is_weekend = dt.weekday() >= 5

    # Värmebehov (kW)
    heating_kw = max(0.0, (17.0 - temp_c) * 0.075)

    # Hushållsel basprofil (kW)
    if is_weekend:
        if 0 <= hour < 7:
            appl_kw = 0.45
        elif 7 <= hour < 10:
            appl_kw = 1.10
        elif 10 <= hour < 17:
            appl_kw = 0.85
        elif 17 <= hour < 22:
            appl_kw = 1.30
        else:
            appl_kw = 0.65
    else:
        if 0 <= hour < 6:
            appl_kw = 0.40
        elif 6 <= hour < 8.5:
            appl_kw = 1.25
        elif 8.5 <= hour < 16.5:
            appl_kw = 0.60
        elif 16.5 <= hour < 21.5:
            appl_kw = 1.45
        else:
            appl_kw = 0.55

    return heating_kw + appl_kw

def main():
    client = HAForecastClient()
    state = load_state()

    # 1. Hämta aktuella realtidsdata
    actual_load_state = client.get_state("sensor.solis_s6_eh1p_household_load_power")
    ev_power_state = client.get_state("sensor.ev_power")
    outdoor_temp_state = client.get_state("sensor.aktuell_utetemperatur_bt1_40004")

    actual_load_w = float(actual_load_state.get("state", 1500)) if actual_load_state else 1500.0
    ev_power_w = float(ev_power_state.get("state", 0)) if (ev_power_state and ev_power_state.get("state") not in ["unknown", "unavailable"]) else 0.0
    net_house_load_kw = max(0.1, (actual_load_w - ev_power_w) / 1000.0)

    outdoor_temp = float(outdoor_temp_state.get("state", 10.0)) if outdoor_temp_state else 10.0

    # 2. Adaptiv felberäkning (EWMA / I-del)
    last_pred = state.get("last_predicted_kw", net_house_load_kw)
    instant_error_kw = net_house_load_kw - last_pred

    # EWMA för integrerande bias (begränsad till +/- 1.5 kW för stabilitet)
    alpha = 0.20  # Inlärningshastighet
    current_bias = state.get("integral_bias_kw", 0.0)
    updated_bias = max(-1.5, min(1.5, (1 - alpha) * current_bias + alpha * instant_error_kw))
    state["integral_bias_kw"] = updated_bias

    # 3. Hämta Solcast solprognos & SMHI väderprognos
    solcast_raw = client.read_solcast_json()
    smhi_hourly = client.get_weather_forecast()

    # Bygg temperaturlookup per timme från SMHI
    temp_by_hour = {}
    for item in smhi_hourly:
        dt_str = item["datetime"]
        dt = datetime.datetime.fromisoformat(dt_str)
        temp_by_hour[dt.strftime("%Y-%m-%d %H")] = float(item["temperature"])

    # Bygg Solcast lookup per halvtimme
    solcast_by_period = {}
    for item in solcast_raw:
        dt_str = item["period_start"]
        dt = datetime.datetime.fromisoformat(dt_str)
        solcast_by_period[dt.strftime("%Y-%m-%d %H:%M")] = float(item["pv_estimate"])

    # 4. Skapa 192 kvartar (48 h)
    now = datetime.datetime.now(datetime.timezone.utc)
    quarters = generate_15m_timestamps(now, steps=192)

    load_forecast_kw = []
    pv_forecast_kw = []

    for dt in quarters:
        hour_key = dt.strftime("%Y-%m-%d %H")
        t_c = temp_by_hour.get(hour_key, outdoor_temp)

        # Baslast + adaptiv bias
        base_kw = base_load_kw(dt, t_c)
        p_load = max(0.2, base_kw + updated_bias)
        load_forecast_kw.append(round(p_load, 3))

        # Solcast: matcha halvtimmen
        half_hour_min = (dt.minute // 30) * 30
        dt_half = dt.replace(minute=half_hour_min, second=0, microsecond=0)
        sol_key = dt_half.strftime("%Y-%m-%d %H:%M")
        p_pv = solcast_by_period.get(sol_key, 0.0)
        pv_forecast_kw.append(round(p_pv, 3))

    # Spara förra prediktionen för nästa kvarts adaptiva jämförelse
    state["last_predicted_kw"] = load_forecast_kw[0]
    state["last_updated_epoch"] = int(time.time())
    save_state(state)

    # 5. Publicera till Home Assistant
    client.post_state(
        "sensor.batterycontrol_load_forecast_now",
        load_forecast_kw[0],
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Batteristyrning Lastprognos Kvart 0",
            "integral_bias_kw": round(updated_bias, 3),
            "actual_house_kw": round(net_house_load_kw, 3),
            "instant_error_kw": round(instant_error_kw, 3),
            "forecast_48h_kw": load_forecast_kw
        }
    )

    client.post_state(
        "sensor.batterycontrol_pv_forecast_now",
        pv_forecast_kw[0],
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Batteristyrning Solprognos Kvart 0",
            "forecast_48h_kw": pv_forecast_kw
        }
    )

    client.close()
    print("Klart! 48h-prognoser genererade och skickade till HA.")
    print(f"Aktuell faktisk huslast: {net_house_load_kw:.2f} kW")
    print(f"Predikterad last kvart 0: {load_forecast_kw[0]:.2f} kW (Adaptiv bias: {updated_bias:+.2f} kW)")
    print(f"Solprognos kvart 0: {pv_forecast_kw[0]:.2f} kW")

if __name__ == "__main__":
    main()
