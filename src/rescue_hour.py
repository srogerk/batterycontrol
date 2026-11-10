#!/usr/bin/env python3
"""
"Rädda timmen" — tar hänsyn till vad innevarande klocktimme REDAN förbrukat.

Problemet (verifierat 2026-11-10): optimize_v2 fick aldrig veta hur mycket
nätenergi som redan gått åt under timmen. LP:n såg bara de kvartar som återstår
och kunde fylla på upp till 13.5 kW i dem. Vid :45 blir det 3.38 kWh på ett
kvart, vilket ensamt ger 4.97 kW timmedel mot ett tak på 1.3 kW.

Lösningen: mät hur mycket import- och exportmätarna rört sig sedan timmens
början, räkna fram den redan uppnådda timmedeleffekten, och berätta för LP:n
vilken medeleffekt de ÅTERSTÅENDE kvartarna måste hålla för att timmedelvärdet
ska landa under tröskeln.

    tillåten_återstående_kw = (tröskel * 1.0 - redan_kwh_netto) / timmar_kvar

Är den negativ kan timmen inte längre räddas med batteriet — då laddar vi ur
maximalt för att begränsa skadan och flaggar det i loggen.

Nettotecknet är I + D - C - E (import + urladdning - laddning - export), samma
konvention som daily_profit.py använder och som verifierats mot huslasten.
"""

import datetime

IMPORT_ENTITY = "sensor.solis_s6_eh1p_meter_total_active_energy_from_grid"
EXPORT_ENTITY = "sensor.solis_s6_eh1p_meter_total_active_energy_to_grid"
SOC_ENTITY = "sensor.solis_s6_eh1p_battery_soc"

# En mätarställning som hoppar bakåt eller framåt orimligt mycket är ingen
# riktig förbrukning — antingen en omstart eller en felaktig avläsning.
MAX_PLAUSIBLE_KWH = 15.0


def _to_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _reading_at_or_before(rows, when):
    """Senaste mätarställning vid eller före `when`."""
    best = None
    target = _to_utc(when)
    for row in rows:
        changed = row.get("last_changed") or row.get("last_updated")
        if not changed:
            continue
        try:
            ts = _to_utc(datetime.datetime.fromisoformat(changed))
            val = float(row.get("state"))
        except (ValueError, TypeError):
            continue
        if ts <= target:
            best = (ts, val)
    return best


def _first_reading(rows):
    """Första giltiga mätarställningen i serien."""
    for row in rows:
        try:
            val = float(row.get("state"))
        except (ValueError, TypeError):
            continue
        changed = row.get("last_changed") or row.get("last_updated")
        try:
            ts = _to_utc(datetime.datetime.fromisoformat(changed))
        except (ValueError, TypeError):
            ts = None
        return ts, val
    return None


def _last_reading(rows):
    for row in reversed(rows):
        try:
            val = float(row.get("state"))
        except (ValueError, TypeError):
            continue
        changed = row.get("last_changed") or row.get("last_updated")
        try:
            ts = _to_utc(datetime.datetime.fromisoformat(changed))
        except (ValueError, TypeError):
            ts = None
        return ts, val
    return None


def hour_progress(client, now, threshold_kw):
    """
    Räkna ut innevarande klocktimmes läge.

    Returnerar en dict:
        applicable     — True om timmen är inne och har kvarvarande kvartar
        threshold_kw   — tröskeln som gäller
        used_net_kwh   — netto nätenergi sedan timmens början (I - E)
        charge_kwh     — batteriladdning sedan timmens början
        discharge_kwh  — batteriurladdning sedan timmens början
        net_kwh        — I + D - C - E (det som belastar timmedelvärdet)
        hours_left     — timmar kvar av timmen (0.25-steg)
        secs_left      — sekunder kvar
        allowed_kw     — medeleffekt de kvarvarande kvartarna får hålla
        achieved_kw    — timmedeleffekt om resten blir noll
        over_kw        — hur mycket över tröskeln vi redan ligger
        salvaged      — False om timmen inte längre kan räddas
    """
    result = {
        "applicable": False, "threshold_kw": threshold_kw, "used_net_kwh": 0.0,
        "charge_kwh": 0.0, "discharge_kwh": 0.0, "net_kwh": 0.0,
        "hours_left": 0.0, "secs_left": 0.0, "allowed_kw": None,
        "achieved_kw": 0.0, "over_kw": 0.0, "salvaged": True, "reason": "",
    }

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + datetime.timedelta(hours=1)
    secs_left = (hour_end - now).total_seconds()
    if secs_left <= 0:
        result["reason"] = "timmen ar slut"
        return result

    result["applicable"] = True
    result["secs_left"] = secs_left
    result["hours_left"] = secs_left / 3600.0

    try:
        start = hour_start - datetime.timedelta(minutes=2)
        imp_rows = client.history(IMPORT_ENTITY, start, now)
        exp_rows = client.history(EXPORT_ENTITY, start, now)
    except Exception as exc:  # nätverksfel ska aldrig stoppa optimeringen
        result["applicable"] = False
        result["reason"] = f"historik ej tillganglig: {exc}"
        return result

    imp_first, imp_last = _first_reading(imp_rows), _last_reading(imp_rows)
    exp_first, exp_last = _first_reading(exp_rows), _last_reading(exp_rows)

    if not imp_first or not imp_last:
        result["applicable"] = False
        result["reason"] = "ingen importmätarserie"
        return result

    used_import = max(0.0, imp_last[1] - imp_first[1])
    used_export = 0.0
    if exp_first and exp_last:
        used_export = max(0.0, exp_last[1] - exp_first[1])

    if used_import > MAX_PLAUSIBLE_KWH or used_export > MAX_PLAUSIBLE_KWH:
        result["applicable"] = False
        result["reason"] = f"orimlig matardifferens ({used_import:.2f} kWh)"
        return result

    result["used_net_kwh"] = used_import - used_export

    # Batteriets egengjorda energi. Laddning från nätet belastar inte nätet
    # (den finns redan i importmätaren), men urladdning minskar importen.
    chg_kwh = dis_kwh = 0.0
    try:
        chg_rows = client.history(
            "sensor.solis_s6_eh1p_battery_charge_power", start, now)
        dis_rows = client.history(
            "sensor.solis_s6_eh1p_battery_discharge_power", start, now)
        chg_kwh = _integrate_power(chg_rows, hour_start, now)
        dis_kwh = _integrate_power(dis_rows, hour_start, now)
    except Exception:
        pass
    result["charge_kwh"] = chg_kwh
    result["discharge_kwh"] = dis_kwh

    # NETTO MOT NÄTET = I - E.
    #
    # Viktigt: batteriets laddning och urladdning behöver INTE läggas till.
    # De syns redan i importmätaren — laddar batteriet 5 kW från nätet stiger I
    # med 5 kW, laddar det ur 5 kW sjunker I. Att addera D - C här skulle
    # dubbelräkna och få timmen att se mycket värre ut än den är.
    #
    # (Formeln I + D - C - E beskriver HUSLAST, inte nätimport. Se daily_profit.py.)
    net = used_import - used_export
    result["net_kwh"] = net

    allowed = (threshold_kw * 1.0 - net) / result["hours_left"]
    result["allowed_kw"] = allowed
    result["achieved_kw"] = net / 1.0
    result["over_kw"] = max(0.0, result["achieved_kw"] - threshold_kw)

    if allowed < 0.0:
        result["salvaged"] = False
        result["reason"] = (
            f"timmen kan inte raddas: {net:.2f} kWh redan, "
            f"tak {threshold_kw:.2f} kW")

    return result


def _integrate_power(rows, start, end):
    """Trapetsintegrera en effektserie (kW) till kWh mellan start och end."""
    points = []
    for row in rows:
        try:
            val = float(row.get("state"))
        except (ValueError, TypeError):
            continue
        changed = row.get("last_changed") or row.get("last_updated")
        try:
            ts = _to_utc(datetime.datetime.fromisoformat(changed))
        except (ValueError, TypeError):
            continue
        points.append((ts, max(0.0, val)))

    if len(points) < 2:
        return 0.0

    points.sort(key=lambda p: p[0])
    total = 0.0
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        a = max(t0, _to_utc(start))
        b = min(t1, _to_utc(end))
        if b <= a:
            continue
        hours = (b - a).total_seconds() / 3600.0
        total += 0.5 * (v0 + v1) * hours
    return total


def apply_to_optimizer(progress, threshold_kw):
    """
    Bygg de två argument optimize_v2 behöver för att respektera timmen.

    Returnerar (hour_used_kwh, hour_threshold_kw) eller (None, None) om
    uppgifterna inte är användbara.
    """
    if not progress.get("applicable") or progress.get("allowed_kw") is None:
        return None, None
    return progress["net_kwh"], threshold_kw


def describe(progress):
    """Kort loggtext."""
    if not progress.get("applicable"):
        return f"[RADDA TIMMEN] hoppar over: {progress.get('reason', 'okand')}"
    mins = int(progress["secs_left"] // 60)
    txt = (f"[RADDA TIMMEN] {mins} min kvar | redan {progress['net_kwh']:.3f} kWh "
           f"({progress['achieved_kw']:.2f} kW medel) | tak {progress['threshold_kw']:.2f} kW "
           f"| aterstaende kvartar far ta {progress['allowed_kw']:.2f} kW")
    if not progress["salvaged"]:
        txt += " | GAR EJ ATT RADDA - urladdning max"
    return txt
