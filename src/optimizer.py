#!/usr/bin/env python3
"""
Global LP-optimerare för batteristyrning (48h MPC, 192 kvartar).
Löser Modell B (separata energiflöden per kvart) med scipy.optimize.linprog (HiGHS).

Parametrar:
- Verkningsgrad: eta_chg = 0.95, eta_dis = 0.95 (roundtrip ~90.25%).
- Cykelkostnad: 0.15 kr/kWh in, 0.15 kr/kWh ut (0.30 kr/kWh roundtrip).
- Kapacitet: 20.0 kWh användbart (0 <= SOC <= 20.0 kWh).
- Effektgränser: max 10 kW laddning (2.5 kWh/kvart), max 15 kW urladdning (3.75 kWh/kvart).
- Hård nätgräns: max 14 kW timmedelvärde.
- Vardagar 07:00: Mål SOC = 20.0 kWh (100% användbart).
- Vardagar 07:00-20:00: Peak shaving mot dynamiskt X med mål 3.0 kWh reserv kl 20:00.
"""

import os
import json
import time
import datetime
import holidays
import hmac
import hashlib
import base64
import numpy as np
import scipy.optimize
import paramiko

SE_HOLIDAYS = holidays.Sweden(years=[2026, 2027])

def is_effekttariff_day(dt: datetime.datetime) -> bool:
    """
    Göteborg Energi Tidsindelad elnätsavgift:
    Effektavgift högpris (135 kr/kW) gäller endast 1 nov - 31 mars, helgfria vardagar 07:00-20:00.
    Övrig tid (april-oktober, helger, röda dagar) har 0 kr/kW i effektavgift.
    """
    d = dt.date()
    if d.month not in [11, 12, 1, 2, 3]:
        return False
    if dt.weekday() >= 5 or d in SE_HOLIDAYS:
        return False
    return True

HA_HOST = "10.12.22.12"
HA_USER = "root"
SSH_KEY = "/workspace/id_ed25519"
REFRESH_TOKEN_ID = "2e524fc4f7254527b734d719720a6230"
JWT_KEY = "4bf3f2bb0d22f9220eb4ca4a90263b81b071c05b86a50e27104dcfe392ee49b71840a81f8bedab36d28450d428e380021d34da8c9a79f9570bc5f7f146b77826"

def generate_ha_token():
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"iss": REFRESH_TOKEN_ID, "iat": now, "exp": now + 3600 * 24 * 30}
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    signing_input = b64(json.dumps(header, separators=(",", ":")).encode()) + "." + b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(JWT_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64(sig)

class HAPriceAndStateClient:
    def __init__(self):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(HA_HOST, username=HA_USER, key_filename=SSH_KEY)
        self.token = generate_ha_token()

    def get_state(self, entity_id):
        cmd = f'curl -s http://172.30.32.1:8123/api/states/{entity_id} -H "Authorization: Bearer {self.token}"'
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        try:
            return json.loads(stdout.read().decode())
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

    def call_service(self, domain, service, service_data=None):
        payload = service_data or {}
        cmd = f'''curl -s -X POST http://172.30.32.1:8123/api/services/{domain}/{service} \\
          -H "Authorization: Bearer {self.token}" \\
          -H "Content-Type: application/json" \\
          -d '{json.dumps(payload)}' '''
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        return stdout.read().decode()

    def close(self):
        self.ssh.close()

def build_price_vector(client, timestamps):
    """Bygger 192-kvartarsvektorer för importpris och exportpris (kr/kWh)."""
    import_data = client.get_state("sensor.nordpool_predbat_import")
    export_data = client.get_state("sensor.nordpool_predbat_export")

    lookup_import = {}
    lookup_export = {}

    if import_data and "attributes" in import_data:
        for entry in import_data["attributes"].get("raw_today", []) + import_data["attributes"].get("raw_tomorrow", []):
            st = entry["start"][:16]  # YYYY-MM-DDTHH:MM
            lookup_import[st] = float(entry["value"])

    if export_data and "attributes" in export_data:
        for entry in export_data["attributes"].get("raw_today", []) + export_data["attributes"].get("raw_tomorrow", []):
            st = entry["start"][:16]
            lookup_export[st] = float(entry["value"])

    p_in = []
    p_out = []
    last_p_in = float(import_data.get("state", 1.5)) if import_data else 1.5
    last_p_out = float(export_data.get("state", 1.0)) if export_data else 1.0

    for dt in timestamps:
        # Konvertera dt till lokal tid (eller ISO sträng)
        # Vi matchar mot YYYY-MM-DDTHH:MM
        key = dt.strftime("%Y-%m-%dT%H:%M")
        val_in = lookup_import.get(key, last_p_in)
        val_out = lookup_export.get(key, last_p_out)
        p_in.append(val_in)
        p_out.append(val_out)
        last_p_in = val_in
        last_p_out = val_out

    return np.array(p_in), np.array(p_out)

def optimize_48h(soc_now_kwh, load_forecast_kw, pv_forecast_kw, p_in, p_out, timestamps):
    """
    Formulerar och löser LP för 192 kvartar.
    
    Variabler per kvart t (7 st per kvart * 192 = 1344 variabler):
    0: E_grid_load (kWh)
    1: E_grid_batt (kWh)
    2: E_batt_load (kWh)
    3: E_batt_grid (kWh)
    4: E_pv_load   (kWh)
    5: E_pv_batt   (kWh)
    6: E_pv_grid   (kWh)
    
    Skalor per kvart: Energi (kWh) = Effekt (kW) * 0.25 h
    """
    T = len(timestamps)
    E_load = np.array(load_forecast_kw) * 0.25
    E_pv = np.array(pv_forecast_kw) * 0.25

    num_vars_per_t = 7
    total_vars = T * num_vars_per_t

    # Parametrar
    eta_chg = 0.95
    eta_dis = 0.95
    c_cyk_in = 0.15   # kr/kWh
    c_cyk_out = 0.15  # kr/kWh
    v_term = np.mean(p_in)  # Terminalvärde kr/kWh

    # Målfunktion: min c^T * x
    c = np.zeros(total_vars)
    for t in range(T):
        idx = t * num_vars_per_t
        # Importkostnad + laddcykel
        c[idx + 0] = p_in[t]                      # grid->load
        c[idx + 1] = p_in[t] + c_cyk_in           # grid->batt
        # Urladdning cykelkostnad
        c[idx + 2] = c_cyk_out                    # batt->load
        c[idx + 3] = c_cyk_out - p_out[t]         # batt->grid (exportvinst)
        # Sol
        c[idx + 4] = 0.0                          # pv->load
        c[idx + 5] = c_cyk_in                     # pv->batt
        c[idx + 6] = -p_out[t]                    # pv->grid (exportvinst)

    # Terminalvärdering av SOC(T):
    # SOC(T) = soc_now + sum_t [ eta_chg*(grid_batt + pv_batt) - (1/eta_dis)*(batt_load + batt_grid) ]
    # Subtrahera v_term * SOC(T) från målfunktionen:
    for t in range(T):
        idx = t * num_vars_per_t
        c[idx + 1] -= v_term * eta_chg            # grid->batt
        c[idx + 5] -= v_term * eta_chg            # pv->batt
        c[idx + 2] += v_term * (1.0 / eta_dis)    # batt->load
        c[idx + 3] += v_term * (1.0 / eta_dis)    # batt->grid

    # Bivillkor för likheter (A_eq * x = b_eq):
    # 1. Huslast: grid_load + batt_load + pv_load = E_load[t]  (T st)
    # 2. Soltillgång: pv_load + pv_batt + pv_grid <= E_pv[t] (kan formuleras som likhet med slack, eller olikhet)
    A_eq = []
    b_eq = []

    for t in range(T):
        idx = t * num_vars_per_t
        row_load = np.zeros(total_vars)
        row_load[idx + 0] = 1.0  # grid_load
        row_load[idx + 2] = 1.0  # batt_load
        row_load[idx + 4] = 1.0  # pv_load
        A_eq.append(row_load)
        b_eq.append(E_load[t])

    # Bivillkor för olikheter (A_ub * x <= b_ub):
    A_ub = []
    b_ub = []

    # 1. Sol: pv_load + pv_batt + pv_grid <= E_pv[t]
    for t in range(T):
        idx = t * num_vars_per_t
        row_pv = np.zeros(total_vars)
        row_pv[idx + 4] = 1.0
        row_pv[idx + 5] = 1.0
        row_pv[idx + 6] = 1.0
        A_ub.append(row_pv)
        b_ub.append(E_pv[t])

    # 2. Max ladd-/urladdningseffekt per kvart (max 10 kW laddning = 2.5 kWh/kvart, max 15 kW urladdning = 3.75 kWh/kvart)
    P_max_chg_quarter = 10.0 * 0.25   # 2.5 kWh / kvart
    P_max_dis_quarter = 15.0 * 0.25   # 3.75 kWh / kvart
    for t in range(T):
        idx = t * num_vars_per_t
        # Laddning: grid_batt + pv_batt <= 2.5
        row_chg = np.zeros(total_vars)
        row_chg[idx + 1] = 1.0
        row_chg[idx + 5] = 1.0
        A_ub.append(row_chg)
        b_ub.append(P_max_chg_quarter)

        # Urladdning: batt_load + batt_grid <= 3.75
        row_dis = np.zeros(total_vars)
        row_dis[idx + 2] = 1.0
        row_dis[idx + 3] = 1.0
        A_ub.append(row_dis)
        b_ub.append(P_max_dis_quarter)

    # 3. SOC gränser: 0.0 <= SOC(k) <= 20.0 kWh för alla k = 1..T
    # SOC(k) = soc_now + sum_{t=0}^{k-1} [ eta_chg*(grid_batt + pv_batt) - (1/eta_dis)*(batt_load + batt_grid) ]
    # SOC(k) <= 20.0  => sum <= 20.0 - soc_now
    # SOC(k) >= 0.0   => -sum <= soc_now
    for k in range(1, T + 1):
        row_soc_upper = np.zeros(total_vars)
        row_soc_lower = np.zeros(total_vars)

        for t in range(k):
            idx = t * num_vars_per_t
            # upper
            row_soc_upper[idx + 1] += eta_chg
            row_soc_upper[idx + 5] += eta_chg
            row_soc_upper[idx + 2] -= (1.0 / eta_dis)
            row_soc_upper[idx + 3] -= (1.0 / eta_dis)

            # lower
            row_soc_lower[idx + 1] -= eta_chg
            row_soc_lower[idx + 5] -= eta_chg
            row_soc_lower[idx + 2] += (1.0 / eta_dis)
            row_soc_lower[idx + 3] += (1.0 / eta_dis)

        A_ub.append(row_soc_upper)
        b_ub.append(20.0 - soc_now_kwh)

        A_ub.append(row_soc_lower)
        b_ub.append(soc_now_kwh - 0.0)

        # Reservkrav under effekttariff-fönstret (07:00 - 20:00 helgfria vardagar nov-mar):
        # Fram till kl 19:00: minst 1.0 kWh i reserv för att parera oväntade hushållstoppar.
        # Efter kl 19:00 (19:00 - 20:00): reserven släpps fri ner mot 0.0 kWh användbart.
        dt_k = timestamps[k - 1]
        if is_effekttariff_day(dt_k):
            # Kl 07:00: Mål SOC >= 19.5 kWh (i princip fullt inför dagens höglast)
            if dt_k.hour == 7 and dt_k.minute == 0:
                row_target_07 = np.zeros(total_vars)
                for t in range(k):
                    idx = t * num_vars_per_t
                    row_target_07[idx + 1] -= eta_chg
                    row_target_07[idx + 5] -= eta_chg
                    row_target_07[idx + 2] += (1.0 / eta_dis)
                    row_target_07[idx + 3] += (1.0 / eta_dis)
                A_ub.append(row_target_07)
                b_ub.append(soc_now_kwh - 19.5)

            # Mellan 07:15 och 19:00: SOC(k) >= 1.0 kWh (reserv för oväntade transienter)
            elif (dt_k.hour > 7 or (dt_k.hour == 7 and dt_k.minute > 0)) and dt_k.hour < 19:
                row_reserve = np.zeros(total_vars)
                for t in range(k):
                    idx = t * num_vars_per_t
                    row_reserve[idx + 1] -= eta_chg
                    row_reserve[idx + 5] -= eta_chg
                    row_reserve[idx + 2] += (1.0 / eta_dis)
                    row_reserve[idx + 3] += (1.0 / eta_dis)
                A_ub.append(row_reserve)
                b_ub.append(soc_now_kwh - 1.0)

            # Kl 19:00 exakt: SOC >= 1.0 kWh (övergångspunkt)
            elif dt_k.hour == 19 and dt_k.minute == 0:
                row_reserve = np.zeros(total_vars)
                for t in range(k):
                    idx = t * num_vars_per_t
                    row_reserve[idx + 1] -= eta_chg
                    row_reserve[idx + 5] -= eta_chg
                    row_reserve[idx + 2] += (1.0 / eta_dis)
                    row_reserve[idx + 3] += (1.0 / eta_dis)
                A_ub.append(row_reserve)
                b_ub.append(soc_now_kwh - 1.0)

            # Efter 19:00 (19:15 - 20:00): Inget extra reservkrav, standardgränsen SOC >= 0 kWh gäller!

    # 4. Hårt nättak (14 kW = 3.5 kWh per kvart)
    for t in range(T):
        idx = t * num_vars_per_t
        row_grid = np.zeros(total_vars)
        row_grid[idx + 0] = 1.0  # grid_load
        row_grid[idx + 1] = 1.0  # grid_batt
        A_ub.append(row_grid)
        b_ub.append(14.0 * 0.25)

    # Gränser: alla variabler >= 0
    bounds = [(0, None) for _ in range(total_vars)]

    res = scipy.optimize.linprog(
        c,
        A_ub=np.array(A_ub),
        b_ub=np.array(b_ub),
        A_eq=np.array(A_eq),
        b_eq=np.array(b_eq),
        bounds=bounds,
        method="highs"
    )

    if not res.success:
        return None, res.message

    # Tolka lösningen
    sol = res.x
    plan = []
    curr_soc = soc_now_kwh

    for t in range(T):
        idx = t * num_vars_per_t
        g_load = sol[idx + 0]
        g_batt = sol[idx + 1]
        b_load = sol[idx + 2]
        b_grid = sol[idx + 3]
        p_load = sol[idx + 4]
        p_batt = sol[idx + 5]
        p_grid = sol[idx + 6]

        curr_soc += eta_chg * (g_batt + p_batt) - (1.0 / eta_dis) * (b_load + b_grid)
        curr_soc = max(0.0, min(20.0, curr_soc))

        p_charge_kw = (g_batt + p_batt) / 0.25
        p_discharge_kw = (b_load + b_grid) / 0.25
        grid_import_kw = (g_load + g_batt) / 0.25
        grid_export_kw = (b_grid + p_grid) / 0.25

        plan.append({
            "datetime": timestamps[t].isoformat(),
            "soc_kwh": round(curr_soc, 2),
            "soc_percent": round((curr_soc / 20.0) * 100.0, 1),
            "charge_kw": round(p_charge_kw, 2),
            "discharge_kw": round(p_discharge_kw, 2),
            "grid_import_kw": round(grid_import_kw, 2),
            "grid_export_kw": round(grid_export_kw, 2)
        })

    return plan, "Optimal"

def main():
    client = HAPriceAndStateClient()

    # Läs aktuellt SOC från Solis
    soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")
    soc_pct = float(soc_state.get("state", 20.0)) if soc_state else 20.0
    soc_kwh = (soc_pct / 100.0) * 20.0

    # Läs prognoser från HA
    load_state = client.get_state("sensor.batterycontrol_load_forecast_now")
    pv_state = client.get_state("sensor.batterycontrol_pv_forecast_now")

    if not load_state or "forecast_48h_kw" not in load_state.get("attributes", {}):
        print("Kunde inte läsa lastprognos från HA. Kör forecast.py först.")
        return

    load_forecast = load_state["attributes"]["forecast_48h_kw"]
    pv_forecast = pv_state["attributes"]["forecast_48h_kw"]

    now = datetime.datetime.now().astimezone()
    minute = (now.minute // 15) * 15
    base = now.replace(minute=minute, second=0, microsecond=0)
    timestamps = [base + datetime.timedelta(minutes=15 * i) for i in range(192)]

    p_in, p_out = build_price_vector(client, timestamps)

    print(f"Startar LP-optimering: SOC={soc_pct:.1f}% ({soc_kwh:.2f} kWh), T={len(timestamps)} kvartar.")
    t0 = time.time()
    plan, status = optimize_48h(soc_kwh, load_forecast, pv_forecast, p_in, p_out, timestamps)
    elapsed = time.time() - t0

    if not plan:
        print(f"Optimeringen misslyckades: {status}")
        return

    print(f"Optimering löst på {elapsed:.3f} s! Status: {status}")
    print(f"Första kvarten ({plan[0]['datetime']}):")
    print(f"  Laddning:    {plan[0]['charge_kw']} kW")
    print(f"  Urladdning:  {plan[0]['discharge_kw']} kW")
    print(f"  Nätimport:   {plan[0]['grid_import_kw']} kW")
    print(f"  Beräknad SOC: {plan[0]['soc_percent']}% ({plan[0]['soc_kwh']} kWh)")

    # Sammanfattning av dygnet
    next_07 = [p for p in plan if "T07:00" in p["datetime"]]
    if next_07:
        print(f"Planerad SOC vid kl 07:00: {next_07[0]['soc_percent']}% ({next_07[0]['soc_kwh']} kWh)")

    # Publicera optimeringsplanen till HA
    client.post_state(
        "sensor.batterycontrol_plan_now",
        "Laddar" if plan[0]["charge_kw"] > 0.1 else ("Laddar ur" if plan[0]["discharge_kw"] > 0.1 else "Väntar"),
        {
            "friendly_name": "Batteristyrning Optimeringsplan",
            "next_charge_kw": plan[0]["charge_kw"],
            "next_discharge_kw": plan[0]["discharge_kw"],
            "target_soc_percent": plan[0]["soc_percent"],
            "solve_time_seconds": round(elapsed, 3),
            "plan_48h": plan
        }
    )

    client.close()
    print("Plan publicerad till sensor.batterycontrol_plan_now i Home Assistant.")

if __name__ == "__main__":
    main()
