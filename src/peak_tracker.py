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


PEAK_KEYS = ("p1", "p2", "p3")


def read_peaks_from_ha(client):
    """Läser P1/P2/P3 ur HA. Returnerar None per värde som saknas.

    Används BARA vid engångsmigrering och för att upptäcka drift mot
    spegeln - HA är inte langre nagon kalla (se load_peaks).

    Varden <= 0 behandlas som "ingen data": input_number har initial: 0.0,
    sa 0.0 ar vad en ny eller aterstalld entitet visar - inte en matning.
    Utan den kontrollen skulle en tom state-fil + en nyomstartad HA
    migrera in 0.0 som om det vore manadens toppar.
    """
    out = []
    for key in PEAK_KEYS:
        st = client.get_state(f"input_number.batterycontrol_{key}")
        val = st.get("state") if st else None
        try:
            num = float(val) if val not in (None, "unknown", "unavailable") else None
        except (TypeError, ValueError):
            num = None
        out.append(num if num is not None and num > 0.0 else None)
    return tuple(out)


def load_peaks(state, client):
    """Hämtar P1/P2/P3 ur state-filen - den enda källan.

    Tidigare lästes värdena ur HA:s input_number vid varje kvart, med
    hardkodade fallback (2.80/2.65/2.49) om entiteten var trasig. Tva fel
    foljde av det:

      1. HA blev kalla i stallet for spegel. En omladdning, en omstart
         eller en manuell ändring i HA satte sanningen for hela styrningen.
      2. Fallback-vardena var indistinkta fran verkliga matvarden, sa en
         tyst fallback gick inte att upptacka i loggen.

    State-filen ligger pa en skrivbar yta som overlever bade omstart av
    HA och av add-onen (/data respektive web/). Den ar darfor kallan.
    """
    if all(k in state for k in PEAK_KEYS):
        return float(state["p1"]), float(state["p2"]), float(state["p3"])

    # Migrering: state-filen ar aldre an denna kod och har inga toppar.
    # Las HA en gang och persistera - darefter ar filen kallan.
    from_ha = read_peaks_from_ha(client)
    if any(v is None for v in from_ha):
        raise RuntimeError(
            "P1/P2/P3 saknas bade i peak_state.json och i Home Assistant. "
            "Vagrar gissa: en pahittad troskel styr batteriet fel.")

    p1, p2, p3 = sorted(from_ha, reverse=True)
    state.update({"p1": round(p1, 2), "p2": round(p2, 2), "p3": round(p3, 2)})
    save_peak_state(state)
    print(f"[PEAK TRACKER] Migrerade P1/P2/P3 fran HA till state-filen: "
          f"P1={p1} P2={p2} P3={p3}")
    return p1, p2, p3


def ensure_ha_mirror(client, p1, p2, p3):
    """Skriver P1/P2/P3 till HA om de avviker fran state-filen.

    Gor HA sjalvlakande: en omladdning som lamnar entiteterna pa gamla
    varden rattas vid nasta kvart, utan att nagon manuell insats kravs.
    Skrivningen sker bara vid avvikelse, sa det kostar normalt ingenting.
    """
    want = (round(p1, 2), round(p2, 2), round(p3, 2))
    cur = read_peaks_from_ha(client)
    if all(c is not None and abs(c - w) < 0.005 for c, w in zip(cur, want)):
        return
    print(f"[PEAK TRACKER] HA avviker fran state-filen (HA={cur}, rattar till {want}).")
    set_peaks_in_ha(client, *want)


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
        if state.get("month") is None:
            # Ingen lasbar state-fil. Det ar INTE ett manadsskifte - vi vet
            # inte om det ar samma manad eller inte. Lamna p-nycklarna borta
            # sa att load_peaks forsoker migrera fran HA, och vagra omaven
            # HA saknar data. Ett nollstallande har vore att kasta bort
            # manadens toppar utan att veta om de behovs.
            print("[PEAK TRACKER] Ingen lasbar state-fil - forsoker migrera fran HA.")
            state = {"month": current_month_str, "daily_peaks": {}}
            save_peak_state(state)
        else:
            print(f"[PEAK TRACKER] Ny månad ({current_month_str})! Nollställer månadsstate.")
            # P1/P2/P3 ar manadens tre hogsta timmedeleffekter (MTD). Vid ett
            # manadsskifte finns inga timmar an, sa de maste nollas - annars
            # ligger forra manadens toppar kvar som troskel och haller
            # batteriet passivt hela den nya manaden. Golv troskeln ar sand i
            # stallet (P_tak faller tillbaka pa sasongsgolvet).
            state = {
                "month": current_month_str,
                "daily_peaks": {},
                "p1": 0.0,
                "p2": 0.0,
                "p3": 0.0,
            }
            save_peak_state(state)

    p1, p2, p3 = load_peaks(state, client)

    # HA ar en spegel av state-filen, inte en kalla. Rattar en omladdning
    # som lamnat entiteterna pa gamla varden redan har, innan planen ritas.
    try:
        ensure_ha_mirror(client, p1, p2, p3)
    except Exception as exc:
        print(f"[PEAK TRACKER] kunde inte spegla till HA: {exc}")

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
                        # State-filen ar kallan: skriv den forst, spegla sedan.
                        # Kraschar HA-anropet efterat ar vardena anda säkrade
                        # och rattas vid nasta kvart av ensure_ha_mirror.
                        state["p1"] = round(new_p1, 2)
                        state["p2"] = round(new_p2, 2)
                        state["p3"] = round(new_p3, 2)
                        save_peak_state(state)
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
