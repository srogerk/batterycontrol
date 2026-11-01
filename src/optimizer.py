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
    """
    Bygger prisvektorer för kända kvartar (kr/kWh).
    
    Dynamisk horisont:
    Avgränsar horisonten strikt till de kvartar som faktiskt är kända från Nord Pool
    (fram till 23:45 idag före kl 13, och fram till 23:45 imorgon efter kl 13).
    Påhittade/upprepade priser i framtiden undviks helt.
    """
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
    valid_timestamps = []

    for dt in timestamps:
        key = dt.strftime("%Y-%m-%dT%H:%M")
        if key in lookup_import:
            valid_timestamps.append(dt)
            p_in.append(lookup_import[key])
            p_out.append(lookup_export.get(key, 0.0))
        else:
            # Så fort prisdata upphör bryter vi horisonten
            break

    # Fallback om inga priser alls hittades i raw_today
    if not valid_timestamps:
        default_p_in = float(import_data.get("state", 1.5)) if import_data else 1.5
        default_p_out = float(export_data.get("state", 1.0)) if export_data else 1.0
        return np.array([default_p_in] * len(timestamps)), np.array([default_p_out] * len(timestamps)), timestamps

    return np.array(p_in), np.array(p_out), valid_timestamps

def optimize_48h(soc_now_kwh, load_forecast_kw, pv_forecast_kw, p_in, p_out, timestamps, prev_charge_kw=0.0):
    """
    Formulerar och löser LP för dynamisk horisont T kvartar (rullande MPC).
    Löser Modell B (separata energiflöden per kvart) med scipy.optimize.linprog (HiGHS).

    Progressiv trappstegsmodell för laddeffekt (5 trappsteg):
      Steg 1 (0 - 4 kW):     Mjuk basladdning (0.00 kr/kWh påslag)
      Steg 2 (4 - 6 kW):     Moderat laddning (+0.08 kr/kWh påslag)
      Steg 3 (6 - 8 kW):     Högre laddning (+0.22 kr/kWh påslag)
      Steg 4 (8 - 10 kW):    Hög laddeffekt (+0.50 kr/kWh påslag)
      Steg 5 (10+ kW):       Effektspik > 10 kW (+100% påslag av spotpriset, d.v.s. dubbelt pris)

    Variabler per kvart t (13 st per kvart * T):
      0: E_grid_load          (kWh)
      1: E_grid_batt_t1       (kWh, steg 1: 0 - 4 kW = max 1.00 kWh/kvart)
      2: E_grid_batt_t2       (kWh, steg 2: 4 - 6 kW = max 0.50 kWh/kvart)
      3: E_grid_batt_t3       (kWh, steg 3: 6 - 8 kW = max 0.50 kWh/kvart)
      4: E_grid_batt_t4       (kWh, steg 4: 8 - 10 kW = max 0.50 kWh/kvart)
      5: E_grid_batt_t5       (kWh, steg 5: 10+ kW = max 1.25 kWh/kvart upp till 15 kW)
      6: E_batt_load          (kWh)
      7: E_batt_grid          (kWh)
      8: E_pv_load            (kWh)
      9: E_pv_batt            (kWh)
      10: E_pv_grid           (kWh)
      11: R_chg_pos           (kWh, ökning i laddeffekt från föregående kvart)
      12: R_chg_neg           (kWh, minskning i laddeffekt från föregående kvart)

    Effekttariff-optimering med dynamiskt effekttak (P_dag):
    - För varje helgfri vardag nov-mar (07:00 - 20:00) skapas en kontinuerlig variabel P_dag_kw.
    - Nätimport per kvart under 07-20 begränsas av P_dag:
        (E_grid_load + sum(E_grid_batt_t_i)) / 0.25 <= P_dag_kw
    - P_dag beläggs med en kostnad på 45 kr/kW (motsvarande 135 kr/kW / 3 månadstoppar).
    - Under P_dag råder fritt spotpris, vilket låter optimeraren arbeta naturligt.
    - Mål: Batteriet laddas fullt till billigaste nattpris före kl 07:00, och under 07-20
      smetas nödvändig nätimport ut helt platt så att effektavgiften minimeras.

    Mjuk laddoptimering och dygnsagnostisk kontinuitet:
    - Kontinuitetshysteres c_hysteresis = 0.05 kr/kWh (5 öre): Stöd för t=0 om laddning pågick.
    - Växlingsdämpning c_switch = 0.03 kr/(kWh ändring): Dämpar växlingar mellan intilliggande kvartar.
    """
    T = len(timestamps)
    E_load = np.array(load_forecast_kw) * 0.25
    E_pv = np.array(pv_forecast_kw) * 0.25

    # Definition av de 5 progressiva laddstegen
    tiers = [
        {"name": "0-4kW",  "cap_kw": 4.0, "penalty_type": "fixed", "val": 0.00},
        {"name": "4-6kW",  "cap_kw": 2.0, "penalty_type": "fixed", "val": 0.08},
        {"name": "6-8kW",  "cap_kw": 2.0, "penalty_type": "fixed", "val": 0.22},
        {"name": "8-10kW", "cap_kw": 2.0, "penalty_type": "fixed", "val": 0.50},
        {"name": "10+kW",  "cap_kw": 5.0, "penalty_type": "relative", "val": 1.00},  # +100% påslag vid >10 kW
    ]
    num_tiers = len(tiers)

    # Identifiera alla unika effekttariff-dagar i planeringshorisonten
    tariff_days = {}  # datum -> lista med kvartsindex t under 07:00-20:00
    for t, dt in enumerate(timestamps):
        if is_effekttariff_day(dt):
            if 7 <= dt.hour < 20:
                d = dt.date()
                if d not in tariff_days:
                    tariff_days[d] = []
                tariff_days[d].append(t)

    tariff_day_list = sorted(list(tariff_days.keys()))
    tariff_day_to_idx = {d: i for i, d in enumerate(tariff_day_list)}
    num_tariff_days = len(tariff_day_list)

    # 1 (grid_load) + num_tiers (grid_batt) + 2 (batt) + 3 (pv) + 2 (ramp) = 13 variabler per kvart
    num_vars_per_t = 1 + num_tiers + 7
    base_vars = T * num_vars_per_t
    total_vars = base_vars + num_tariff_days

    # Parametrar
    eta_chg = 0.95
    eta_dis = 0.95
    c_cyk_in = 0.15          # kr/kWh slitagekostnad in
    c_cyk_out = 0.15         # kr/kWh slitagekostnad ut
    c_hysteresis = 0.05      # kr/kWh kontinuitetsstöd i t=0 om laddning pågår
    c_switch = 0.03          # kr per kWh ändring i laddeffekt mellan kvartar
    v_term = np.mean(p_in)   # Terminalvärde kr/kWh

    was_charging = (prev_charge_kw > 0.05)

    # Målfunktion: min c^T * x
    c = np.zeros(total_vars)
    for t in range(T):
        idx = t * num_vars_per_t
        h_disc = c_hysteresis if (t == 0 and was_charging) else 0.0

        # Nät till huslast
        c[idx + 0] = p_in[t]

        # Nät till batteri med progressiva påslag
        for i, tier in enumerate(tiers):
            if tier["penalty_type"] == "fixed":
                pen = tier["val"]
            else:
                pen = p_in[t] * tier["val"]  # Relativt påslag (100% för 10+ kW)
            c[idx + 1 + i] = p_in[t] + c_cyk_in + pen - h_disc

        # Batteri urladdning
        b_idx = idx + 1 + num_tiers
        c[b_idx + 0] = c_cyk_out                                            # batt->load
        c[b_idx + 1] = c_cyk_out - p_out[t]                                 # batt->grid (exportvinst)

        # Sol
        c[b_idx + 2] = 0.0                                                  # pv->load
        c[b_idx + 3] = c_cyk_in                                             # pv->batt
        c[b_idx + 4] = -p_out[t]                                            # pv->grid (exportvinst)

        # Växlingsdämpning mellan intilliggande kvartar
        c[b_idx + 5] = c_switch
        c[b_idx + 6] = c_switch

    # Terminalvärdering av SOC(T)
    for t in range(T):
        idx = t * num_vars_per_t
        for i in range(num_tiers):
            c[idx + 1 + i] -= v_term * eta_chg                              # grid->batt_t_i
        b_idx = idx + 1 + num_tiers
        c[b_idx + 3] -= v_term * eta_chg                                    # pv->batt
        c[b_idx + 0] += v_term * (1.0 / eta_dis)                            # batt->load
        c[b_idx + 1] += v_term * (1.0 / eta_dis)                            # batt->grid

    # Tariff-kostnad för månadstopp P_dag (45 kr/kW per toppdag)
    for day_idx in range(num_tariff_days):
        c[base_vars + day_idx] = 45.0

    # Bivillkor för likheter (A_eq * x = b_eq):
    A_eq = []
    b_eq = []

    # 1. Huslast: grid_load + batt_load + pv_load = E_load[t]
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row_load = np.zeros(total_vars)
        row_load[idx + 0] = 1.0     # grid_load
        row_load[b_idx + 0] = 1.0   # batt_load
        row_load[b_idx + 2] = 1.0   # pv_load
        A_eq.append(row_load)
        b_eq.append(E_load[t])

    # 2. Kontinuitet & mjuk laddrampning:
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row_ramp = np.zeros(total_vars)
        for i in range(num_tiers):
            row_ramp[idx + 1 + i] = 1.0

        if t == 0:
            p_prev_quarter = (prev_charge_kw * 0.25) if was_charging else 0.0
            row_ramp[b_idx + 5] = -1.0
            row_ramp[b_idx + 6] = 1.0
            A_eq.append(row_ramp)
            b_eq.append(p_prev_quarter)
        else:
            prev_idx = (t - 1) * num_vars_per_t
            for i in range(num_tiers):
                row_ramp[prev_idx + 1 + i] = -1.0
            row_ramp[b_idx + 5] = -1.0
            row_ramp[b_idx + 6] = 1.0
            A_eq.append(row_ramp)
            b_eq.append(0.0)

    # Bivillkor för olikheter (A_ub * x <= b_ub):
    A_ub = []
    b_ub = []

    # 1. Sol: pv_load + pv_batt + pv_grid <= E_pv[t]
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row_pv = np.zeros(total_vars)
        row_pv[b_idx + 2] = 1.0
        row_pv[b_idx + 3] = 1.0
        row_pv[b_idx + 4] = 1.0
        A_ub.append(row_pv)
        b_ub.append(E_pv[t])

    # 2. Max laddeffekter per steg och totalt:
    P_max_dis_quarter = 15.0 * 0.25   # 3.75 kWh / kvart urladdning

    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers

        # Begränsning per laddsteg
        for i, tier in enumerate(tiers):
            row_cap = np.zeros(total_vars)
            row_cap[idx + 1 + i] = 1.0
            A_ub.append(row_cap)
            b_ub.append(tier["cap_kw"] * 0.25)

        # Urladdning: batt_load + batt_grid <= 3.75 kWh (15.0 kW)
        row_dis = np.zeros(total_vars)
        row_dis[b_idx + 0] = 1.0
        row_dis[b_idx + 1] = 1.0
        A_ub.append(row_dis)
        b_ub.append(P_max_dis_quarter)

    # 3. Batteriets energibalans & SOC-gränser:
    for k in range(1, T + 1):
        row_soc_upper = np.zeros(total_vars)
        row_soc_lower = np.zeros(total_vars)

        for t in range(k):
            idx = t * num_vars_per_t
            b_idx = idx + 1 + num_tiers

            # Laddning ökar SOC
            for i in range(num_tiers):
                row_soc_upper[idx + 1 + i] += eta_chg
                row_soc_lower[idx + 1 + i] -= eta_chg
            row_soc_upper[b_idx + 3] += eta_chg
            row_soc_lower[b_idx + 3] -= eta_chg

            # Urladdning minskar SOC
            row_soc_upper[b_idx + 0] -= (1.0 / eta_dis)
            row_soc_lower[b_idx + 0] += (1.0 / eta_dis)
            row_soc_upper[b_idx + 1] -= (1.0 / eta_dis)
            row_soc_lower[b_idx + 1] += (1.0 / eta_dis)

        # SOC(k) <= 20.0 kWh
        A_ub.append(row_soc_upper)
        b_ub.append(20.0 - soc_now_kwh)

        # SOC(k) >= 0.0 kWh
        A_ub.append(row_soc_lower)
        b_ub.append(soc_now_kwh - 0.0)

        # Reservkrav under effekttariff-fönstret (07:00 - 19:00 helgfria vardagar nov-mar):
        dt_k = timestamps[k - 1]
        if is_effekttariff_day(dt_k) and 7 <= dt_k.hour < 19:
            A_ub.append(row_soc_lower)
            b_ub.append(soc_now_kwh - 1.0)

    # 4. Hård nätgräns: Max 14 kW import per kvart
    for t in range(T):
        idx = t * num_vars_per_t
        row_grid = np.zeros(total_vars)
        row_grid[idx + 0] = 1.0  # grid_load
        for i in range(num_tiers):
            row_grid[idx + 1 + i] = 1.0
        A_ub.append(row_grid)
        b_ub.append(14.0 * 0.25)

    # 5. Effekttak P_dag under effekttariff (07:00 - 20:00):
    for d, indices in tariff_days.items():
        day_idx = tariff_day_to_idx[d]
        p_dag_var_idx = base_vars + day_idx
        for t in indices:
            idx = t * num_vars_per_t
            row_pdag = np.zeros(total_vars)
            row_pdag[idx + 0] = 1.0
            for i in range(num_tiers):
                row_pdag[idx + 1 + i] = 1.0
            row_pdag[p_dag_var_idx] = -0.25
            A_ub.append(row_pdag)
            b_ub.append(0.0)

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
        return None, res.message, {}

    # Tolka lösningen
    sol = res.x
    plan = []
    curr_soc = soc_now_kwh

    # Extrahera P_dag per tariffdag
    optimal_p_dag = {}
    for day_idx, d in enumerate(tariff_day_list):
        optimal_p_dag[d.isoformat()] = round(float(sol[base_vars + day_idx]), 2)

    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers

        g_load = sol[idx + 0]
        g_batt = sum(sol[idx + 1 + i] for i in range(num_tiers))

        b_load = sol[b_idx + 0]
        b_grid = sol[b_idx + 1]
        p_load = sol[b_idx + 2]
        p_batt = sol[b_idx + 3]
        p_grid = sol[b_idx + 4]

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

    return plan, "Optimal", optimal_p_dag


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

    p_in, p_out, valid_timestamps = build_price_vector(client, timestamps)
    T = len(valid_timestamps)
    load_f = load_forecast[:T]
    pv_f = pv_forecast[:T]

    print(f"Startar LP-optimering: SOC={soc_pct:.1f}% ({soc_kwh:.2f} kWh), dynamisk horisont T={T} kvartar ({T/4:.2f} h).")
    t0 = time.time()
        # Hämta förra kvartens planerade laddning för kontinuitetshysteres
    plan_now_state = client.get_state("sensor.batterycontrol_plan_now")
    prev_charge_kw = 0.0
    if plan_now_state and "next_charge_kw" in plan_now_state.get("attributes", {}):
        prev_charge_kw = float(plan_now_state["attributes"].get("next_charge_kw", 0.0))

    plan, status, optimal_p_dag = optimize_48h(soc_kwh, load_f, pv_f, p_in, p_out, valid_timestamps, prev_charge_kw=prev_charge_kw)
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
            "optimal_p_dag": optimal_p_dag,
            "plan_48h": plan
        }
    )

    client.close()
    print("Plan publicerad till sensor.batterycontrol_plan_now i Home Assistant.")

if __name__ == "__main__":
    main()
