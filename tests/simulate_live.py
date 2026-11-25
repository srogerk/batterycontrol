#!/usr/bin/env python3
"""Simulerar styrningen mot den LEVANDE planen - utan att skriva nagonting.

Laser sensor.batterycontrol_plan_now, valjer lage for nuvarande kvart och visar
exakt vilka entitetsskrivningar run_loop skulle gora just nu. Ingenting skickas.

Kor: python3 tests/simulate_live.py
     python3 tests/simulate_live.py --kvart 3   (3 kvartar framat)
"""

import datetime
import os
import sys
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/tmp")

from src import battery_control as bc  # noqa: E402

import ha  # noqa: E402


def is_effekttariff_day(dt):
    """Samma regel som optimizer_v2.is_effekttariff_day (nov-mars, vardag)."""
    if dt.month not in (11, 12, 1, 2, 3):
        return False
    return dt.weekday() < 5


def main():
    offset = 0
    if "--kvart" in sys.argv:
        offset = int(sys.argv[sys.argv.index("--kvart") + 1])

    ha.connect()
    plan = ha.states().get("sensor.batterycontrol_plan_now")
    if not plan:
        print("FEL: sensor.batterycontrol_plan_now saknas")
        return 1

    attrs = plan.get("attributes") or {}
    tz = zoneinfo.ZoneInfo("Europe/Stockholm")
    now = datetime.datetime.now(tz)
    if offset:
        now = now + datetime.timedelta(minutes=15 * offset)

    charge_kw = attrs.get("next_grid_charge_kw", 0.0)
    discharge_kw = attrs.get("next_discharge_kw", 0.0)
    grid_import_kw = attrs.get("next_grid_import_kw", 0.0)
    is_tariff = is_effekttariff_day(now) and (7 <= now.hour < 20)

    volt_state = ha.states().get("sensor.solis_s6_eh1p_battery_voltage") or {}
    try:
        voltage = float(volt_state.get("state"))
    except (TypeError, ValueError):
        voltage = 360.0
    if voltage < 100 or voltage > 500:
        voltage = 360.0

    mode = bc.choose_mode(
        charge_kw=charge_kw, discharge_kw=discharge_kw, is_tariff_window=is_tariff
    )
    actions = bc.build_actions(
        mode, charge_kw=charge_kw, discharge_kw=discharge_kw,
        grid_import_kw=grid_import_kw, voltage=voltage, now=now,
    )

    print()
    print("=" * 76)
    print("  SIMULERING mot levande plan - INGET SKRIVS")
    print("=" * 76)
    print(f"  Tid:               {now:%Y-%m-%d %H:%M:%S} {now.tzname()}")
    print(f"  Planstatus:        {plan.get('state')}")
    print(f"  Tariffonster:      {'JA' if is_tariff else 'nej'}"
          f"  ({'effekttariffdag' if is_effekttariff_day(now) else 'ej tariffdag'},"
          f" timme {now.hour})")
    print(f"  Plan: laddning {charge_kw:.2f} kW | urladdning {discharge_kw:.2f} kW"
          f" | natimport {grid_import_kw:.2f} kW")
    print(f"  Batterispanning:   {voltage:.1f} V")
    print(f"  -> STYRLAGE:       {bc.describe(mode)}  [{mode}]")
    print("-" * 76)
    for i, a in enumerate(actions, 1):
        ent = a["data"]["entity_id"].replace("solis_s6_eh1p_", "")
        if a["service"] == "turn_on":
            val = "ON"
        elif a["service"] == "turn_off":
            val = "OFF"
        else:
            val = a["data"].get("value", a["data"].get("time"))
        print(f"  {i:2d}. {ent:56s} = {val}")
    print("-" * 76)
    print(f"  {len(actions)} skrivningar skulle goras.")
    print("=" * 76)
    print()

    # Varningar
    if is_tariff and mode in (bc.MODE_DISCHARGE_GRID, bc.MODE_IDLE_GRID):
        if grid_import_kw <= 0.01:
            print("  ⚠️  VARNING: tariffonster men planens natimport ar 0 -"
                  " peak-shaving-taket blir 0 W och huslasten gar pa batteriet.")
    if not is_tariff and mode == bc.MODE_CHARGE:
        print("  ℹ️  Laddning utanfor tariffonstret - tillatet, billigare natt.")
    ha.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
