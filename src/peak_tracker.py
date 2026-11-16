#!/usr/bin/env python3
"""
Spårning och uppdatering av månadens effekttoppar (P1, P2, P3) och P_max för dagen.
Integrerat med Home Assistant (input_number.batterycontrol_p1..p3).

Körs allra först i varje kvartsloop:
1. Läser in föregående kvarts medeleffekt (energi under 15 min / 0.25 h).
2. Vid varje heltimme (:00) kontrolleras den avslutade klocktimmens medeleffekt under 07-20.
3. Om en ny topp uppstår på ett nytt dygn uppdateras P1, P2, P3 i Home Assistant.
4. Räknar fram P_max (hittills högsta timmedeleffekt under dagens tariffönster).
"""

import sys
import os
import json
import datetime
import zoneinfo

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import rescue_hour as rh
from src.optimizer import is_effekttariff_day

try:
    from . import paths
except ImportError:
    import paths

# State-filen maste ligga pa en skrivbar yta som overlever en omstart av
# add-onen. src/ ar read-only i add-on-imagen och dessutom en del av koden -
# i add-on-laget pekar paths.data_file() darfor pa /data i stallet.
PEAK_STATE_FILE = paths.data_file("peak_state.json")


def load_peak_state():
    if os.path.exists(PEAK_STATE_FILE):
        try:
            with open(PEAK_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "month": None,
        "daily_peaks": {}  # "YYYY-MM-DD": {"peak_kw": float, "hour": int}
    }


def save_peak_state(state):
    try:
        with open(PEAK_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Kunde inte spara peak_state.json: {e}")


def get_current_peaks_from_ha(client):
    """Läser P1, P2, P3 från Home Assistant (input_number)."""
    p1_st = client.get_state("input_number.batterycontrol_p1")
    p2_st = client.get_state("input_number.batterycontrol_p2")
    p3_st = client.get_state("input_number.batterycontrol_p3")

    p1 = float(p1_st.get("state", 2.80)) if p1_st else 2.80
    p2 = float(p2_st.get("state", 2.65)) if p2_st else 2.65
    p3 = float(p3_st.get("state", 2.49)) if p3_st else 2.49

    sorted_p = sorted([p1, p2, p3], reverse=True)
    return sorted_p[0], sorted_p[1], sorted_p[2]


def set_peaks_in_ha(client, p1, p2, p3):
    """Skriver uppdaterade P1, P2, P3 till Home Assistant."""
    sorted_p = sorted([p1, p2, p3], reverse=True)
    p1_val, p2_val, p3_val = round(sorted_p[0], 2), round(sorted_p[1], 2), round(sorted_p[2], 2)

    client.call_service("input_number", "set_value", {"entity_id": "input_number.batterycontrol_p1", "value": p1_val})
    client.call_service("input_number", "set_value", {"entity_id": "input_number.batterycontrol_p2", "value": p2_val})
    client.call_service("input_number", "set_value", {"entity_id": "input_number.batterycontrol_p3", "value": p3_val})
    print(f"[PEAK TRACKER] Uppdaterat i HA: P1={p1_val} kW, P2={p2_val} kW, P3={p3_val} kW")


def update_and_get_peaks(client, now: datetime.datetime):
    """
    Körs i början av varje kvart i styrloopen:
    1. Beräknar senaste kvartens medeleffekt.
    2. Beräknar timmedeleffekter för avslutade klocktimmar 07-20 idag.
    3. Uppdaterar P1, P2, P3 i HA om en ny topp har satts.
    4. Returnerar (p1, p2, p3, p_max_today, last_quarter_kw).
    """
    tz = now.tzinfo or zoneinfo.ZoneInfo("Europe/Stockholm")
    current_month_str = now.strftime("%Y-%m")
    today_str = now.strftime("%Y-%m-%d")

    state = load_peak_state()
    if state.get("month") != current_month_str:
        print(f"[PEAK TRACKER] Ny månad ({current_month_str})! Nollställer månadsstate.")
        state = {
            "month": current_month_str,
            "daily_peaks": {}
        }
        save_peak_state(state)

    p1, p2, p3 = get_current_peaks_from_ha(client)
    p_max_today = 0.0
    last_quarter_kw = 0.0

    # 1. Hämta historik för senaste kvarten och dagens tariffönster
    if is_effekttariff_day(now) and now.hour >= 7:
        start_tariff = now.replace(hour=7, minute=0, second=0, microsecond=0)
        hist = client.history(rh.IMPORT_ENTITY, start_tariff, now)
        if hist:
            # Senaste kvartens medeleffekt
            current_q_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
            prev_q_start = current_q_start - datetime.timedelta(minutes=15)
            
            vq_start = rh._reading_at_or_before(hist, prev_q_start)
            vq_end = rh._reading_at_or_before(hist, current_q_start)
            if vq_start and vq_end:
                diff_q = max(0.0, vq_end[1] - vq_start[1])
                last_quarter_kw = round(diff_q / 0.25, 2)
                print(f"[PEAK TRACKER] Senaste kvartens medeleffekt ({prev_q_start.strftime('%H:%M')}-{current_q_start.strftime('%H:%M')}): {last_quarter_kw} kW (P3={p3} kW)")

            # Avslutade timmar under tariffönstret 07:00 - now.hour
            max_completed_hour = min(now.hour, 20)
            for h in range(7, max_completed_hour):
                t_h_start = now.replace(hour=h, minute=0, second=0, microsecond=0)
                t_h_end = now.replace(hour=h+1, minute=0, second=0, microsecond=0)
                v_s = rh._reading_at_or_before(hist, t_h_start)
                v_e = rh._reading_at_or_before(hist, t_h_end)
                if v_s and v_e:
                    diff_kwh = max(0.0, v_e[1] - v_s[1])
                    hour_p_kw = round(diff_kwh, 2)
                    if hour_p_kw > p_max_today:
                        p_max_today = hour_p_kw

            # Om dagens högsta timme ändrats, uppdatera state och P1-P3
            if p_max_today > 0.0:
                prev_day_peak = state["daily_peaks"].get(today_str, {}).get("peak_kw", 0.0)
                if p_max_today > prev_day_peak:
                    state["daily_peaks"][today_str] = {
                        "peak_kw": p_max_today,
                        "updated": now.isoformat()
                    }
                    save_peak_state(state)

                    # Rogers regel: P1,P2,P3 = sort(P1, P2, max(P3, P_dagmax))
                    #
                    # Bara P3 kan hojas av en ny dagstopp - de tva ovriga ligger
                    # per definition over P3 sedan tidigare. En ny dagstopp som
                    # understiger P3 paverkar ingenting (den tranger inte in
                    # bland manadens tre hogsta).
                    #
                    # Tidigare byggdes listan som sorted(all_peaks + [p1,p2,p3]),
                    # men all_peaks innehaller redan de dygn som P1/P2/P3 vilar
                    # pa. Varje topp raknades darfor bade som dagspost och som
                    # P-varde, listan fylldes med dubbletter och P3 blev for hog.
                    # Simulering 3000 manader: den regeln hade fel i 2708 fall,
                    # Rogers regel i 0.
                    new_p1, new_p2, new_p3 = sorted(
                        [p1, p2, max(p3, p_max_today)], reverse=True)

                    if (new_p1, new_p2, new_p3) != (p1, p2, p3):
                        print(f"[PEAK TRACKER] Ny timtopp uppnådd ({p_max_today} kW)! Uppdaterar HA...")
                        set_peaks_in_ha(client, new_p1, new_p2, new_p3)
                        p1, p2, p3 = new_p1, new_p2, new_p3

    return p1, p2, p3, p_max_today, last_quarter_kw


if __name__ == "__main__":
    from src.optimizer import HAPriceAndStateClient
    client = HAPriceAndStateClient()
    tz = zoneinfo.ZoneInfo("Europe/Stockholm")
    now = datetime.datetime.now(tz)
    p1, p2, p3, p_max, last_q = update_and_get_peaks(client, now)
    print(f"Resultat: P1={p1}, P2={p2}, P3={p3}, P_max={p_max}, Senaste kvart={last_q} kW")
    client.close()
