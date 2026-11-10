"""
Optimizer v2 for Batterycontrol
Implementerar specifikation november 2026:
- Batterikapacitet: 25 kWh nominellt, 85% användbart (15% - 100%, dvs 3.75 kWh - 25.0 kWh).
- Cyklingskostnad: 0.30 kr/kWh totalt (0.15 kr in, 0.15 kr ut).
- Inköp: spotpris m moms + avgifter (ca 0.50 kr/kWh).
- Sälj: spotpris x moms + nätnytta (0.07 kr/kWh). Ingen aktiv batteriexport (Self-Use: batt täcker huslast).
- Max laddeffekt / urladdningseffekt batteri: 10 kW.
- Max timmedeleffekt nät: 13.5 kW (marginal mot avtal 14 kW).
- Progressivt laddeffektpåslag (0.04 kr per kW effekt) för mjuk och utspridd laddning.
- Elbilsladdning: 3 sammanhängande timmar à 5.5 kW (8A x 3 x 240V) mellan 22:00 och 07:00 som ren nätimport.
- Effekttariff: 1 nov - 31 mars, helgfria vardagar 07:00-20:00.
  Pris över tröskel = 45 kr/kW mätt per timmedelvärde (minut 00-00).
  Tröskel = max(P_tak, P_3, P_eft_max, P_eft_prognos).
"""

import sys
import os
import json
import time
import datetime
import zoneinfo
import holidays
import numpy as np
import scipy.optimize

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.optimizer import HAPriceAndStateClient, build_price_vector, is_effekttariff_day, get_p_dag_floor_kw
from src import ev_block

# Konstanter enligt spec
NOMINAL_CAPACITY_KWH = 25.0
USABLE_MIN_SOC_PCT = 15.0     # 15% hälsogolv
USABLE_MAX_SOC_PCT = 100.0    # 100% fullt
SOC_MIN_KWH = (USABLE_MIN_SOC_PCT / 100.0) * NOMINAL_CAPACITY_KWH   # 3.75 kWh
SOC_MAX_KWH = (USABLE_MAX_SOC_PCT / 100.0) * NOMINAL_CAPACITY_KWH   # 25.00 kWh

C_CYK_IN = 0.15               # kr/kWh cyklingskostnad in
C_CYK_OUT = 0.15              # kr/kWh cyklingskostnad ut
ETA_CHG = 0.95                # Laddverkningsgrad
ETA_DIS = 0.95                # Urladdningsverkningsgrad

MAX_BATTERY_KW = 10.0         # Max laddeffekt & urladdningseffekt mot batteriet
MAX_GRID_HOUR_KW = 13.5       # Högsta tillåtna timmedeleffekt från nätet

P_TARIFF_RATE = 45.0          # kr/kW per toppdag


def get_tariff_floor_p_tak(dt: datetime.datetime) -> float:
    """Säsongsgolv P_tak: 1.3 kW (nov, feb, mar), 1.6 kW (dec, jan)."""
    return get_p_dag_floor_kw(dt)


def optimize_v2(soc_now_kwh: float,
                 load_forecast_kw: list,
                 pv_forecast_kw: list,
                 p_in: list,
                 p_out: list,
                 timestamps: list,
                 p3_historical_kw: float = 0.0,
                 force_effekttariff_tomorrow: bool = False,
                 ev_charge_ordered: bool = False,
                 ev_booking: dict = None,
                 ev_now: datetime.datetime = None,
                 prev_charge_kw: float = 0.0):
    """
    Kör QP/HiGHS optimering enligt Specifikation 2026-11.
    """
    T = len(timestamps)
    if T == 0:
        return None, "empty_horizon", 0.0, {}

    E_load = np.array(load_forecast_kw) * 0.25
    E_pv = np.array(pv_forecast_kw) * 0.25

    # 1. Identifiera timblock för nätgräns (13.5 kW) och effekttariff
    # Gruppera kvartar efter kalendertimme
    hour_map = {}
    for t, dt in enumerate(timestamps):
        h_key = (dt.date(), dt.hour)
        if h_key not in hour_map:
            hour_map[h_key] = []
        hour_map[h_key].append(t)

    # 2. Elbilsallokering: FAST block (se src/ev_block.py).
    # Blocket väljs en gång och sparas; vid varje kvart minskar bara det
    # återstående behovet. Detta förhindrar att fönstret glider framför sig.
    #
    # ev_booking kan skickas in av anroparen (run_loop) som redan löst bokningen;
    # annars byggs arrayen från ev_booking direkt.
    ev_grid_kw = np.zeros(T)
    if ev_booking is not None:
        ev_grid_kw = ev_block.build_ev_array(timestamps, ev_booking, ev_now or timestamps[0])
    elif ev_charge_ordered:
        # Bakåtkompatibel väg: ingen bokning angiven, men order finns.
        # resolve_booking returnerar (booking, is_new) — packa upp, annars får
        # build_ev_array en tuple och kastar TypeError.
        booking, _ = ev_block.resolve_booking(
            timestamps, p_in, ev_now or timestamps[0], True)
        if booking is not None:
            ev_grid_kw = ev_block.build_ev_array(
                timestamps, booking, ev_now or timestamps[0])

    # 3. Förbereda P_eft_prognos och P_tröskel för varje effekttariff-dag
    # Dagar som ingår
    tariff_days = set()
    for dt in timestamps:
        is_eff = is_effekttariff_day(dt)
        if force_effekttariff_tomorrow:
            # Tvinga aktiv effekttariff under 07-20 för alla ingående dagar i test/simulering
            if 7 <= dt.hour < 20:
                is_eff = True
        if is_eff and 7 <= dt.hour < 20:
            tariff_days.add(dt.date())

    sorted_tariff_days = sorted(list(tariff_days))
    day_to_idx = {d: i for i, d in enumerate(sorted_tariff_days)}

    # Beräkna P_eft_prognos per tariffdag
    day_eft_prognos = {}
    for d in sorted_tariff_days:
        day_q_indices = [t for t, ts in enumerate(timestamps) if ts.date() == d and 7 <= ts.hour < 20]
        if day_q_indices:
            tot_load_eft = sum(E_load[t] for t in day_q_indices)
            tot_pv_eft = sum(E_pv[t] for t in day_q_indices)
            # Tillgänglig energi från batteri (räkna med att vi har upp till 20 kWh användbart)
            avail_batt = max(0.0, soc_now_kwh - SOC_MIN_KWH)
            net_energy_needed = max(0.0, tot_load_eft - tot_pv_eft - avail_batt)
            hours_eft = len(day_q_indices) * 0.25
            p_eft_prognos = net_energy_needed / max(1.0, hours_eft)
        else:
            p_eft_prognos = 0.0

        sample_dt = datetime.datetime.combine(d, datetime.time(12, 0), tzinfo=timestamps[0].tzinfo)
        p_tak = get_tariff_floor_p_tak(sample_dt)
        p_thresh = max(p_tak, p3_historical_kw, p_eft_prognos)
        day_eft_prognos[d] = {
            "p_tak": p_tak,
            "p_eft_prognos": p_eft_prognos,
            "p_threshold": p_thresh
        }

    # 4. Formulering av optimeringsvariabler:
    # Per kvart t:
    # 0: E_grid_load (kWh)
    # 1..5: E_grid_batt_tier[0..4] (kWh) -> 5 konvexa segment för 0-2, 2-4, 4-6, 6-8, 8-10 kW
    # 6: E_batt_load (kWh)
    # 7: E_pv_load (kWh)
    # 8: E_pv_batt (kWh)
    # 9: E_pv_grid (kWh)
    # 10: dP_chg (kWh ändring för växlingsdämpning)
    num_tiers = 5
    tier_caps_kw = [2.0, 2.0, 2.0, 2.0, 2.0]     # Varje steg är 2 kW (totalt 10 kW)
    tier_penalties = [0.04, 0.12, 0.20, 0.28, 0.36] # 0.04 kr/kW ökande per steg

    num_vars_per_t = 1 + num_tiers + 4 + 1  # 1 (grid_load) + 5 (tiers) + 4 (batt_load, pv_load, pv_batt, pv_grid) + 1 (dP)
    base_vars = T * num_vars_per_t

    # Slackvariabler för effekttariff: en variabel P_peak_excess per tariffdag
    num_tariff_days = len(sorted_tariff_days)
    total_vars = base_vars + num_tariff_days

    c = np.zeros(total_vars)
    # Terminalvärde för batteriets kvarvarande energi vid horisontens slut:
    # Värderas till det förväntade nattpriset vid/efter horisonten (sista kända kvartens elpris),
    # snarare än dygnsmedelvärdet. Detta förhindrar att optimeraren sparar batterienergi mot dyr
    # dagel eller panikladdar till 100% före midnatt.
    v_term = float(p_in[-1])

    # Kostnader per variabel
    for t in range(T):
        idx = t * num_vars_per_t
        # E_grid_load: betalar spotpris in
        c[idx + 0] = p_in[t]

        # E_grid_batt_tier_i: betalar spotpris in + cyklingskostnad in + progressivt kW-påslag - terminalvärde
        for i in range(num_tiers):
            c[idx + 1 + i] = p_in[t] + C_CYK_IN + tier_penalties[i] - (v_term * ETA_CHG)

        b_idx = idx + 1 + num_tiers
        # E_batt_load: cyklingskostnad ut + ersätter terminalvärde (att tömma batteriet kostar framtida terminalvärde)
        c[b_idx + 0] = C_CYK_OUT + (v_term / ETA_DIS)

        # Sol
        c[b_idx + 1] = 0.0                           # pv->load (gratis)
        c[b_idx + 2] = C_CYK_IN - (v_term * ETA_CHG) # pv->batt (cykling in men bygger terminalvärde)
        c[b_idx + 3] = -p_out[t]                     # pv->grid (intäkt!)

        # Växlingsdämpning dP
        c[b_idx + 4] = 0.02                          # 2 öre per kWh ändring mellan kvartar

    # Kostnad för att överskrida tröskeln under effekttariff
    for d_idx in range(num_tariff_days):
        c[base_vars + d_idx] = P_TARIFF_RATE

    # Bivillkor
    A_eq = []
    b_eq = []
    A_ub = []
    b_ub = []

    # 1. Husets lastbalans varje kvart: E_grid_load + E_batt_load + E_pv_load == E_load[t]
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row = np.zeros(total_vars)
        row[idx + 0] = 1.0     # grid_load
        row[b_idx + 0] = 1.0   # batt_load
        row[b_idx + 1] = 1.0   # pv_load
        A_eq.append(row)
        b_eq.append(E_load[t])

    # 2. Solens balans varje kvart: E_pv_load + E_pv_batt + E_pv_grid == E_pv[t]
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row = np.zeros(total_vars)
        row[b_idx + 1] = 1.0   # pv_load
        row[b_idx + 2] = 1.0   # pv_batt
        row[b_idx + 3] = 1.0   # pv_grid
        A_eq.append(row)
        b_eq.append(E_pv[t])

    # 3. Växlingsdämpning: dP_chg[t] >= |E_batt_chg[t] - E_batt_chg[t-1]|
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row_pos = np.zeros(total_vars)
        row_neg = np.zeros(total_vars)

        row_pos[b_idx + 4] = -1.0
        row_neg[b_idx + 4] = -1.0

        for i in range(num_tiers):
            row_pos[idx + 1 + i] += 1.0
            row_neg[idx + 1 + i] -= 1.0

        if t > 0:
            prev_idx = (t - 1) * num_vars_per_t
            for i in range(num_tiers):
                row_pos[prev_idx + 1 + i] -= 1.0
                row_neg[prev_idx + 1 + i] += 1.0
            A_ub.append(row_pos)
            b_ub.append(0.0)
            A_ub.append(row_neg)
            b_ub.append(0.0)
        else:
            # t == 0: jämför med prev_charge_kw
            prev_e = prev_charge_kw * 0.25
            A_ub.append(row_pos)
            b_ub.append(prev_e)
            A_ub.append(row_neg)
            b_ub.append(-prev_e)

    # 4. Batteriets maxgräns urladdning per kvart: E_batt_load <= 10 kW * 0.25 = 2.5 kWh
    for t in range(T):
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers
        row = np.zeros(total_vars)
        row[b_idx + 0] = 1.0
        A_ub.append(row)
        b_ub.append(MAX_BATTERY_KW * 0.25)

    # 5. SOC-ackumulering och gränser (3.75 kWh <= SOC <= 25.0 kWh)
    # SOC(k) = SOC_now + sum_{t=0}^{k-1} [ ETA_CHG*(grid_batt + pv_batt) - (1/ETA_DIS)*batt_load ]
    for k in range(1, T + 1):
        row_upper = np.zeros(total_vars)
        row_lower = np.zeros(total_vars)
        for t in range(k):
            idx = t * num_vars_per_t
            b_idx = idx + 1 + num_tiers
            for i in range(num_tiers):
                row_upper[idx + 1 + i] += ETA_CHG
                row_lower[idx + 1 + i] -= ETA_CHG
            row_upper[b_idx + 2] += ETA_CHG     # pv_batt
            row_lower[b_idx + 2] -= ETA_CHG
            row_upper[b_idx + 0] -= (1.0 / ETA_DIS) # batt_load
            row_lower[b_idx + 0] += (1.0 / ETA_DIS)

        # SOC(k) <= SOC_MAX_KWH
        A_ub.append(row_upper)
        b_ub.append(SOC_MAX_KWH - soc_now_kwh)

        # SOC(k) >= SOC_MIN_KWH
        A_ub.append(row_lower)
        b_ub.append(soc_now_kwh - SOC_MIN_KWH)

    # 6. Max timmedeleffekt nät: Högst 13.5 kW över varje klocktimme
    # Timmedeleffekt = sum_{q in timme} (E_grid_load + sum(E_grid_batt) + E_ev_grid) <= 13.5 kWh
    for h_key, quarters in hour_map.items():
        row_hour = np.zeros(total_vars)
        ev_energy_hour = sum(ev_grid_kw[q] * 0.25 for q in quarters)
        for q in quarters:
            idx = q * num_vars_per_t
            row_hour[idx + 0] += 1.0 # grid_load
            for i in range(num_tiers):
                row_hour[idx + 1 + i] += 1.0 # grid_batt

        # Skala så gränsen blir i medeleffekt (kW): sum(E) / (len(quarters)*0.25) <= 13.5 kW
        # dvs sum(E) <= 13.5 * len(quarters) * 0.25
        hour_len_h = len(quarters) * 0.25
        A_ub.append(row_hour)
        b_ub.append(MAX_GRID_HOUR_KW * hour_len_h - ev_energy_hour)

    # 7. Effekttariff: timmedeleffekt under 07-20 vardagar <= P_tröskel + P_peak_excess
    for h_key, quarters in hour_map.items():
        d, h = h_key
        # Kontrollera om denna timme är under aktiv effekttariff
        is_eff = (d in sorted_tariff_days and 7 <= h < 20)
        if is_eff:
            d_idx = day_to_idx[d]
            p_thresh = day_eft_prognos[d]["p_threshold"]
            hour_len_h = len(quarters) * 0.25

            row_eff = np.zeros(total_vars)
            ev_energy_hour = sum(ev_grid_kw[q] * 0.25 for q in quarters)
            for q in quarters:
                idx = q * num_vars_per_t
                row_eff[idx + 0] += 1.0 # grid_load
                for i in range(num_tiers):
                    row_eff[idx + 1 + i] += 1.0 # grid_batt

            # Timmedeleffekt: sum(E) / hour_len_h <= p_thresh + P_peak_excess
            # <=> sum(E) - hour_len_h * P_peak_excess <= p_thresh * hour_len_h
            row_eff[base_vars + d_idx] = -hour_len_h
            A_ub.append(row_eff)
            b_ub.append(p_thresh * hour_len_h - ev_energy_hour)

    # Variabelgränser (bounds)
    bounds = []
    for t in range(T):
        idx = t * num_vars_per_t
        bounds.append((0.0, None)) # grid_load
        for i in range(num_tiers):
            bounds.append((0.0, tier_caps_kw[i] * 0.25)) # grid_batt tiers (max 2 kW per tier = 0.5 kWh)
        bounds.append((0.0, MAX_BATTERY_KW * 0.25))     # batt_load
        bounds.append((0.0, None))                     # pv_load
        bounds.append((0.0, MAX_BATTERY_KW * 0.25))     # pv_batt
        bounds.append((0.0, None))                     # pv_grid
        bounds.append((0.0, None))                     # dP_chg

    for d_idx in range(num_tariff_days):
        bounds.append((0.0, None))                     # P_peak_excess

    # Kör HiGHS linjär/konvex optimerare
    res = scipy.optimize.linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        return None, f"failed: {res.message}", 0.0, day_eft_prognos

    sol = res.x

    # Tolka resultat och bygg plan
    plan = []
    curr_soc = soc_now_kwh

    for t in range(T):
        dt = timestamps[t]
        idx = t * num_vars_per_t
        b_idx = idx + 1 + num_tiers

        g_load = sol[idx + 0]
        g_batt = sum(sol[idx + 1 + i] for i in range(num_tiers))
        b_load = sol[b_idx + 0]
        p_load = sol[b_idx + 1]
        p_batt = sol[b_idx + 2]
        p_grid = sol[b_idx + 3]
        ev_kw = ev_grid_kw[t]

        # SOC vid kvartens start
        soc_start_kwh = curr_soc
        soc_start_pct = (soc_start_kwh / NOMINAL_CAPACITY_KWH) * 100.0

        # Beräkna SOC vid kvartens slut
        curr_soc += ETA_CHG * (g_batt + p_batt) - (1.0 / ETA_DIS) * b_load
        curr_soc = max(SOC_MIN_KWH, min(SOC_MAX_KWH, curr_soc))
        soc_end_kwh = curr_soc
        soc_end_pct = (soc_end_kwh / NOMINAL_CAPACITY_KWH) * 100.0

        grid_imp_kw = (g_load + g_batt) / 0.25 + ev_kw
        grid_chg_kw = g_batt / 0.25
        dis_kw = b_load / 0.25
        pv_exp_kw = p_grid / 0.25

        d_str = dt.date()
        p_thresh = day_eft_prognos[d_str]["p_threshold"] if d_str in day_eft_prognos else None

        plan.append({
            "time": dt.isoformat(),
            "time_str": dt.strftime("%a %H:%M"),
            "hour": dt.hour,
            "minute": dt.minute,
            "load_kw": round(float(load_forecast_kw[t]), 2),
            "pv_kw": round(float(pv_forecast_kw[t]), 2),
            "ev_grid_kw": round(float(ev_kw), 2),
            "grid_import_kw": round(float(grid_imp_kw), 2),
            "grid_charge_kw": round(float(grid_chg_kw), 2),
            "discharge_kw": round(float(dis_kw), 2),
            "grid_export_kw": round(float(pv_exp_kw), 2),
            "soc_kwh": round(float(soc_start_kwh), 2),
            "soc_pct": round(float(soc_start_pct), 1),
            "soc_end_kwh": round(float(soc_end_kwh), 2),
            "soc_end_pct": round(float(soc_end_pct), 1),
            "price_in": round(float(p_in[t]), 3),
            "price_out": round(float(p_out[t]), 3),
            "tariff_threshold_kw": round(float(p_thresh), 2) if p_thresh is not None else None
        })

    return plan, "optimal", res.fun, day_eft_prognos


def main():
    """Huvudfunktion för körning av v2 LP-optimering och publicering till Home Assistant."""
    client = HAPriceAndStateClient()

    # 1. Läs aktuellt SOC från Solis
    soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")
    soc_pct = float(soc_state.get("state", 20.0)) if soc_state else 20.0
    soc_kwh = (soc_pct / 100.0) * NOMINAL_CAPACITY_KWH

    # 2. Läs prognoser från HA
    load_state = client.get_state("sensor.batterycontrol_load_forecast_now")
    pv_state = client.get_state("sensor.batterycontrol_pv_forecast_now")

    if not load_state or "forecast_48h_kw" not in load_state.get("attributes", {}):
        print("Kunde inte läsa lastprognos från HA. Kör forecast.py först.")
        client.close()
        return

    load_forecast = load_state["attributes"]["forecast_48h_kw"]
    pv_forecast = pv_state["attributes"]["forecast_48h_kw"] if pv_state else [0.0]*192

    tz_local = zoneinfo.ZoneInfo("Europe/Stockholm")
    now = datetime.datetime.now(tz_local)
    minute = (now.minute // 15) * 15
    base = now.replace(minute=minute, second=0, microsecond=0)
    timestamps = [base + datetime.timedelta(minutes=15 * i) for i in range(192)]

    p_in, p_out, valid_timestamps = build_price_vector(client, timestamps)
    T = len(valid_timestamps)
    load_f = load_forecast[:T]
    pv_f = pv_forecast[:T]

    # 3. Kontrollera elbilsorder från HA (input_boolean.ev_charge_order_10kwh)
    ev_order_state = client.get_state("input_boolean.ev_charge_order_10kwh")
    ev_order_active = (ev_order_state and ev_order_state.get("state") == "on")

    # 3b. Lös elbilsblocket EN gång och spara det. Fönstret är fast förankrat
    #     i tiden — bara det återstående behovet krymper per kvart.
    ev_booking = None
    if ev_order_active:
        order_id_attr = (ev_order_state.get("attributes") or {}).get("order_id")
        ev_booking, ev_is_new = ev_block.resolve_booking(
            valid_timestamps, p_in, now, True, order_id=order_id_attr)
        if ev_is_new and ev_booking:
            print(f"[ELBIL] Nytt laddblock bokat: "
                  f"{ev_booking['start']:%Y-%m-%d %H:%M} – {ev_booking['end']:%H:%M} "
                  f"({ev_booking['kw']} kW)")
        elif ev_booking:
            rem, elapsed = ev_block.quarters_remaining(ev_booking, now)
            print(f"[ELBIL] Fast block {ev_booking['start']:%H:%M}–{ev_booking['end']:%H:%M}, "
                  f"{rem} kvartar kvar ({rem*0.25:.2f} h), {elapsed} förbrukade")
    else:
        # Order av: glöm gammal bokning så nästa order får ett färskt block.
        ev_block.clear_booking()

    print(f"Startar v2 HiGHS LP-optimering: SOC={soc_pct:.1f}% ({soc_kwh:.2f} kWh), horisont T={T} kvartar ({T/4:.2f} h).")
    t0 = time.time()

    # P3 historisk topp från Göteborg Energi tariff
    p3_hist = 2.0

    plan, status, obj_val, day_eft = optimize_v2(
        soc_now_kwh=soc_kwh,
        load_forecast_kw=load_f,
        pv_forecast_kw=pv_f,
        p_in=p_in,
        p_out=p_out,
        timestamps=valid_timestamps,
        p3_historical_kw=p3_hist,
        ev_charge_ordered=ev_order_active,
        ev_booking=ev_booking,
        ev_now=now
    )
    elapsed = time.time() - t0

    if not plan:
        print(f"Optimeringen misslyckades: {status}")
        client.close()
        return

    print(f"Optimering löst på {elapsed:.3f} s! Status: {status}")
    q0 = plan[0]
    q0_start = valid_timestamps[0].strftime("%H:%M")
    q0_end = (valid_timestamps[0] + datetime.timedelta(minutes=15)).strftime("%H:%M")
    print(f"Första kvarten ({q0_start} -> {q0_end}, {valid_timestamps[0].strftime('%Y-%m-%d')}):")
    print(f"  Nätladdning:      {q0['grid_charge_kw']} kW")
    print(f"  Urladdning:       {q0['discharge_kw']} kW")
    print(f"  Nätimport:        {q0['grid_import_kw']} kW")
    print(f"  Elbil:            {q0['ev_grid_kw']} kW")
    print(f"  Beräknad SOC:     {q0['soc_pct']}% -> {q0.get('soc_end_pct', q0['soc_pct'])}%")

    # Konvertera plan för bakåtkompatibilitet med v1 sensorer och plot
    plan_compat = []
    for p in plan:
        p_comp = dict(p)
        p_comp["datetime"] = p["time"]
        p_comp["charge_kw"] = p["grid_charge_kw"]  # v2: ren nätladdning
        p_comp["soc_percent"] = p["soc_pct"]
        plan_compat.append(p_comp)

    ev_charge_now = (q0["ev_grid_kw"] > 0.05)
    ev_plan_quarters = [p["time"] for p in plan if p["ev_grid_kw"] > 0.05]

    optimal_p_dag = max([d_info.get("p_threshold", 0.0) for d_info in day_eft.values()] or [0.0])

    # Publicera optimeringsplanen till HA
    client.post_state(
        "sensor.batterycontrol_plan_now",
        "Laddar" if q0["grid_charge_kw"] > 0.1 else ("Laddar ur" if q0["discharge_kw"] > 0.1 else "Väntar"),
        {
            "friendly_name": "Batteristyrning Optimeringsplan",
            "next_charge_kw": q0["grid_charge_kw"],
            "next_grid_charge_kw": q0["grid_charge_kw"],
            "next_pv_charge_kw": 0.0,
            "next_discharge_kw": q0["discharge_kw"],
            "target_soc_percent": q0["soc_pct"],
            "solve_time_seconds": round(elapsed, 3),
            "optimal_p_dag": optimal_p_dag,
            "ev_charge_order_active": ev_order_active,
            "ev_charge_now": ev_charge_now,
            "ev_plan_quarters": ev_plan_quarters,
            "day_eft_prognos": {str(k): v for k, v in day_eft.items()},
            "plan_48h": plan_compat
        }
    )

    client.close()
    print("Plan publicerad till sensor.batterycontrol_plan_now i Home Assistant.")


if __name__ == "__main__":
    main()
