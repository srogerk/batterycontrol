#!/usr/bin/env python3
"""
Dygnsvis vinstberäkning (Metod A) för kortet "ESTIMERAD VINST (DYGN)".

Förverkligade kvart läses ur HA-historik (kumulativa elmatare + batterieffekter),
kommande kvart ur optimeringsplanen (quarter_data från generate_web.py).
Ingen ackumulatorfil behövs för det löpande — varje körning räknar om hela dygnet
ur historiken, så ett borttappat cron-kvart reparerar sig självt.

Per kvart q (förverkligat):
    I_q, E_q  = differenser av kumulativa matare (import/export), clampade >= 0
    C_q, D_q  = integraler av batteriets ladd-/urladdningseffekt (kWh)
    N_q       = I_q - E_q + D_q - C_q      (nettolast: huslast - sol)
    Baslinje  = max(0, N_q)*p_in - max(0, -N_q)*p_out   (kostnad utan batteri)
    Brutto_q  = Baslinje - (I_q*p_in - E_q*p_out)       (kvartets vinst)
    Slitage_q = 0.15 kr/kWh * D_q

Dygn (Metod A):
    Nettovinst = Σ Brutto_q (förverkligat + planerat) + ΔLager - Slitage
    ΔLager     = E_slut*P_term - E_start*P_ladd
    P_term     = spotpris sista kvartet, P_ladd = snittpris natt (00-05)

Kortets stora tal "Idag / Imorgon" = dygnets BRUTTO (hittills + prognos kvar),
medan "Går: X kr" = gårdagens slutresultat (netto). Allt redovisas i detaljraden.

Persistens: web/daily_profit.json arkiverar gårdagens slutresultat.
"""

import datetime
import json
import os
import zoneinfo

TZ = zoneinfo.ZoneInfo("Europe/Stockholm")

NOMINAL_CAPACITY_KWH = 25.0
WEAR_KR_PER_KWH = 0.15

HIST_IMPORT = "sensor.solis_s6_eh1p_meter_total_active_energy_from_grid"
HIST_EXPORT = "sensor.solis_s6_eh1p_meter_total_active_energy_to_grid"
HIST_CHG = "sensor.solis_s6_eh1p_battery_charge_power"
HIST_DIS = "sensor.solis_s6_eh1p_battery_discharge_power"
HIST_SOC = "sensor.solis_s6_eh1p_battery_soc"
PRICE_IMPORT = "sensor.nordpool_predbat_import"
PRICE_EXPORT = "sensor.nordpool_predbat_export"

def _default_archive():
    """Arkivet hör till data-ytan: /data i en add-on, web/ i utvecklingsläge."""
    try:
        try:
            from . import paths          # importerad som src.daily_profit
        except ImportError:
            import paths                 # importerad med src/ pa sys.path
        return paths.data_file("daily_profit.json")
    except Exception:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web", "daily_profit.json")


DEFAULT_ARCHIVE = _default_archive()


# ---------------------------------------------------------------- hjälpfunktioner

def _utc_iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hist(client, entity_id, start_iso, end_iso):
    """Hämta HA-history (minimal_response) för en sensor mellan två UTC-tider.

    Går via klientens history()-metod så att båda backends (SSH och Supervisor) fungerar.
    """
    return client.history(entity_id, start_iso, end_iso)


def _f(rec):
    try:
        return float(rec["state"])
    except (KeyError, TypeError, ValueError):
        return None


def _prep(recs):
    """Normalisera history-svar till sorterade [(datetime, float)] (ej numeriska bort)."""
    out = []
    for r in recs:
        v = _f(r)
        if v is None:
            continue
        try:
            out.append((datetime.datetime.fromisoformat(r["last_changed"]), v))
        except (KeyError, TypeError, ValueError):
            pass
    out.sort(key=lambda x: x[0])
    return out


def _value_at(prep, t):
    """Senaste avläsning vid eller före t (för kumulativa mätare)."""
    best = None
    for tt, v in prep:
        if tt <= t:
            best = v
        else:
            break
    return best


def _integrate_kwh(prep, t0, t1):
    """Integrera effektsensor (W) till kWh över [t0, t1)."""
    total = 0.0
    prev_t = None
    prev_v = None
    for tt, v in prep:
        if tt <= t0:
            prev_t, prev_v = tt, v
            continue
        if tt >= t1:
            break
        if prev_t is not None:
            total += (prev_v / 1000.0) * ((tt - prev_t).total_seconds() / 3600.0)
        prev_t, prev_v = tt, v
    if prev_t is not None and prev_t < t1:
        total += (prev_v / 1000.0) * ((t1 - max(prev_t, t0)).total_seconds() / 3600.0)
    return total


def _price_at(prep, t):
    """
    Priset för kvartet som startar vid t.
    predbat-sensorns state = aktuellt kvartspris (stegfunktion), så senaste
    post vid/strax efter kvartstart gäller; max 30 min gammalt godtas.
    """
    limit = t + datetime.timedelta(seconds=45)
    best = None
    for tt, v in prep:
        if tt <= limit:
            best = (tt, v)
        else:
            break
    if best is None:
        return None
    if t - best[0] > datetime.timedelta(minutes=30):
        return None
    return best[1]


def _soc_kwh_at(soc_prep, t):
    v = _value_at(soc_prep, t)
    return None if v is None else v / 100.0 * NOMINAL_CAPACITY_KWH


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------- kvartssummor

def _realized_window(w0, w1, imp, exp, chg, dis, precs_in, precs_out):
    """Summera förverkligade kvart över [w0, w1) i lokal tid."""
    brutto = 0.0
    wear = 0.0
    night_prices = []
    p_term = None
    used = 0
    n = int((w1 - w0).total_seconds() // 900)
    for k in range(n):
        t0 = w0 + datetime.timedelta(minutes=15 * k)
        t1 = t0 + datetime.timedelta(minutes=15)
        i0, i1 = _value_at(imp, t0), _value_at(imp, t1)
        e0, e1 = _value_at(exp, t0), _value_at(exp, t1)
        if None in (i0, i1, e0, e1):
            continue
        pi = _price_at(precs_in, t0)
        if pi is None:
            continue
        po = _price_at(precs_out, t0)
        if po is None:
            po = 0.0
        c = _integrate_kwh(chg, t0, t1)
        d = _integrate_kwh(dis, t0, t1)
        # Mätaromstart-skydd: negativa differenser clampas till 0
        i_q = max(0.0, i1 - i0)
        e_q = max(0.0, e1 - e0)
        n_q = i_q - e_q + d - c
        base = max(0.0, n_q) * pi - max(0.0, -n_q) * po
        brutto += base - (i_q * pi - e_q * po)
        wear += WEAR_KR_PER_KWH * d
        used += 1
        p_term = pi
        if t0.hour < 6:
            night_prices.append(pi)
    return {
        "cashflow_kr": brutto,
        "wear_kr": wear,
        "p_term": p_term,
        "night_prices": night_prices,
        "quarters": used,
    }


def _planned_window(quarters):
    """Summera planerade kvart (quarter_data-dictar från generate_web.py)."""
    brutto = 0.0
    wear = 0.0
    night_prices = []
    p_term = None
    soc_end = None
    for q in quarters:
        pi = float(q.get("price_in", 0.0))
        po = float(q.get("price_out", 0.0))
        n_q = (float(q["load_kw"]) - float(q["pv_kw"])) * 0.25
        base = max(0.0, n_q) * pi - max(0.0, -n_q) * po
        cash = float(q["grid_import_kw"]) * 0.25 * pi - float(q["grid_export_kw"]) * 0.25 * po
        brutto += base - cash
        wear += WEAR_KR_PER_KWH * float(q["discharge_kw"]) * 0.25
        p_term = pi
        soc_end = float(q.get("soc_end_kwh", q.get("soc_kwh", 0.0)))
        hour = q.get("hour")
        if hour is None:
            hour = datetime.datetime.fromisoformat(q["time"]).astimezone(TZ).hour
        if hour < 6:
            night_prices.append(pi)
    return {
        "cashflow_kr": brutto,
        "wear_kr": wear,
        "p_term": p_term,
        "night_prices": night_prices,
        "soc_end_kwh": soc_end,
    }


# ---------------------------------------------------------------- arkiv

def _archive(path, date, block):
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    data[str(date)] = block
    for k in sorted(data)[:-14]:
        data.pop(k)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


# ---------------------------------------------------------------- huvudberäkning

def compute_daily_profit(client, quarter_data, now_local=None, archive_path=DEFAULT_ARCHIVE):
    """
    Beräkna dygnskortets tal.

    Returnerar:
      {
        "today":    {realized_kr, forecast_kr, cashflow_kr, inventory_kr, wear_kr, net_kr},
        "tomorrow": {cashflow_kr, inventory_kr, wear_kr, net_kr} | None,
        "yesterday": {cashflow_kr, inventory_kr, wear_kr, net_kr}
      }
    """
    now = now_local or datetime.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)

    d0 = datetime.datetime.combine(today, datetime.time(0, 0), tzinfo=TZ)
    y0 = datetime.datetime.combine(yesterday, datetime.time(0, 0), tzinfo=TZ)

    # Hela kvart som passerat räknas som förverkligade
    n_done = int((now - d0).total_seconds() // 900)
    realized_end = d0 + datetime.timedelta(minutes=15 * n_done)

    # Historik för gårdagen + idag i ett svep
    hs = _utc_iso(y0 - datetime.timedelta(hours=1))
    he = _utc_iso(now + datetime.timedelta(minutes=5))
    imp = _prep(_hist(client, HIST_IMPORT, hs, he))
    exp = _prep(_hist(client, HIST_EXPORT, hs, he))
    chg = _prep(_hist(client, HIST_CHG, hs, he))
    dis = _prep(_hist(client, HIST_DIS, hs, he))
    soc = _prep(_hist(client, HIST_SOC, hs, he))
    precs_in = _prep(_hist(client, PRICE_IMPORT, hs, he))
    precs_out = _prep(_hist(client, PRICE_EXPORT, hs, he))

    # ---- Idag: förverkligat + planerat
    real = _realized_window(d0, realized_end, imp, exp, chg, dis, precs_in, precs_out)

    def _local(q):
        return datetime.datetime.fromisoformat(q["time"]).astimezone(TZ)

    plan_today = [q for q in quarter_data if _local(q).date() == today and _local(q) >= realized_end]
    plan_tom = [q for q in quarter_data if _local(q).date() == tomorrow]
    plan_t = _planned_window(plan_today) if plan_today else {
        "cashflow_kr": 0.0, "wear_kr": 0.0, "p_term": None, "night_prices": [], "soc_end_kwh": None}

    e_start_today = _soc_kwh_at(soc, d0)
    if plan_t["soc_end_kwh"] is not None:
        e_end_today = plan_t["soc_end_kwh"]
    else:
        e_end_today = _soc_kwh_at(soc, realized_end)
    p_term_today = plan_t["p_term"] if plan_t["p_term"] is not None else real["p_term"]
    night_today = real["night_prices"] or plan_t["night_prices"]
    p_ladd_today = _mean(night_today)

    cash_today = real["cashflow_kr"] + plan_t["cashflow_kr"]
    wear_today = real["wear_kr"] + plan_t["wear_kr"]
    inv_today = 0.0
    if None not in (e_start_today, e_end_today, p_term_today, p_ladd_today):
        inv_today = e_end_today * p_term_today - e_start_today * p_ladd_today
    net_today = cash_today + inv_today - wear_today

    today_block = {
        "realized_kr": round(real["cashflow_kr"], 2),
        "forecast_kr": round(plan_t["cashflow_kr"], 2),
        "cashflow_kr": round(cash_today, 2),
        "inventory_kr": round(inv_today, 2),
        "wear_kr": round(wear_today, 2),
        "net_kr": round(net_today, 2),
    }

    # ---- Imorgon: enbart planen (visas först när morgondagens priser finns)
    tomorrow_block = None
    if plan_tom:
        plan_m = _planned_window(plan_tom)
        e_start_m = e_end_today
        e_end_m = plan_m["soc_end_kwh"]
        p_term_m = plan_m["p_term"]
        p_ladd_m = _mean(plan_m["night_prices"]) or p_term_m
        inv_m = 0.0
        if None not in (e_start_m, e_end_m, p_term_m, p_ladd_m):
            inv_m = e_end_m * p_term_m - e_start_m * p_ladd_m
        net_m = plan_m["cashflow_kr"] + inv_m - plan_m["wear_kr"]
        tomorrow_block = {
            "cashflow_kr": round(plan_m["cashflow_kr"], 2),
            "inventory_kr": round(inv_m, 2),
            "wear_kr": round(plan_m["wear_kr"], 2),
            "net_kr": round(net_m, 2),
        }

    # ---- Går: helt förverkligat dygn + arkivering
    real_y = _realized_window(y0, d0, imp, exp, chg, dis, precs_in, precs_out)
    e_start_y = _soc_kwh_at(soc, y0)
    e_end_y = e_start_today
    p_term_y = real_y["p_term"]
    p_ladd_y = _mean(real_y["night_prices"]) or p_term_y
    inv_y = 0.0
    if None not in (e_start_y, e_end_y, p_term_y, p_ladd_y):
        inv_y = e_end_y * p_term_y - e_start_y * p_ladd_y
    net_y = real_y["cashflow_kr"] + inv_y - real_y["wear_kr"]
    yesterday_block = {
        "cashflow_kr": round(real_y["cashflow_kr"], 2),
        "inventory_kr": round(inv_y, 2),
        "wear_kr": round(real_y["wear_kr"], 2),
        "net_kr": round(net_y, 2),
    }
    try:
        _archive(archive_path, yesterday, yesterday_block)
    except OSError as e:
        print(f"Varning: kunde inte arkivera gårdagens dygn: {e}")

    return {"today": today_block, "tomorrow": tomorrow_block, "yesterday": yesterday_block}


if __name__ == "__main__":
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import src.optimizer as opt

    client = opt.HAPriceAndStateClient()
    plan_state = client.get_state("sensor.batterycontrol_plan_now")
    plan = plan_state.get("attributes", {}).get("plan_48h", []) if plan_state else []
    qc = [{
        "time": p["datetime"],
        "load_kw": p.get("load_kw", 0.0),
        "pv_kw": p.get("pv_kw", 0.0),
        "price_in": p.get("price_in", 0.0),
        "price_out": p.get("price_out", 0.0),
        "grid_import_kw": p.get("grid_import_kw", 0.0),
        "grid_export_kw": p.get("grid_export_kw", 0.0),
        "discharge_kw": p.get("discharge_kw", 0.0),
        "soc_kwh": p.get("soc_kwh", 0.0),
        "soc_end_kwh": p.get("soc_end_kwh", p.get("soc_kwh", 0.0)),
    } for p in plan]
    res = compute_daily_profit(client, qc)
    client.close()
    print(json.dumps(res, indent=2))
