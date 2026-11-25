#!/usr/bin/env python3
"""Visar exakt vilka entitetsskrivningar varje styrmode (spec 7 a-e) ger.

Kor: python3 tests/demo_modes.py

Ingen kontakt med HA, ingen vaxelriktare. `build_actions()` ar ren.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import battery_control as bc  # noqa: E402

NOW = datetime.datetime(2026, 11, 23, 12, 30, 0)
VOLTAGE = 400.0

CASES = [
    (bc.MODE_CHARGE, dict(charge_kw=8.0), "spec 7a"),
    (bc.MODE_DISCHARGE_BATTERY, dict(discharge_kw=4.0), "spec 7b"),
    (bc.MODE_DISCHARGE_GRID, dict(discharge_kw=4.0, grid_import_kw=2.0), "spec 7c"),
    (bc.MODE_IDLE, dict(), "spec 7d"),
    (bc.MODE_IDLE_GRID, dict(), "spec 7e"),
]


def show(mode, kwargs, spec_ref):
    actions = bc.build_actions(mode, now=NOW, voltage=VOLTAGE, **kwargs)
    print()
    print("=" * 78)
    print(f"  {spec_ref}: {bc.describe(mode)}")
    print(f"  indata: {kwargs or 'inget'}  |  tid {NOW:%H:%M:%S}  |  {VOLTAGE:.0f} V")
    print("=" * 78)
    for i, a in enumerate(actions, 1):
        ent = a["data"]["entity_id"]
        if a["service"] == "turn_on":
            val = "ON"
        elif a["service"] == "turn_off":
            val = "OFF"
        else:
            val = a["data"].get("value", a["data"].get("time"))
        short = ent.replace("solis_s6_eh1p_", "").replace("solis_s6_eh3p_", "")
        print(f"  {i:2d}. {a['domain']:6s}.{a['service']:10s} {short:52s} = {val}")
        print(f"      -> {a['why']}")
    print()
    print(f"  {len(actions)} skrivningar.")


def main():
    print()
    print("STYRLOGIK - BATTERIKONTROLL (spec 7)")
    print("Vaxelriktare: Solis S6-EH1P, via solis_modbus i Home Assistant")
    print(f"Genererad: {datetime.datetime.now():%Y-%m-%d %H:%M}")

    # Visa ocksa valet: hur planens kvartar mappas till lage.
    print()
    print("-" * 78)
    print("  MODEVAL (choose_mode)")
    print("-" * 78)
    print(f"  {'laddning':>10s} {'urladdn':>9s} {'tariff':>7s}  ->  lage")
    for chg, dis, tariff in [
        (8.0, 0.0, True), (8.0, 0.0, False),
        (0.0, 4.0, True), (0.0, 4.0, False),
        (0.0, 0.0, True), (0.0, 0.0, False),
    ]:
        mode = bc.choose_mode(
            charge_kw=chg, discharge_kw=dis, is_tariff_window=tariff
        )
        print(f"  {chg:9.1f}kW {dis:8.1f}kW {('ja' if tariff else 'nej'):>7s}"
              f"  ->  {bc.describe(mode)}")

    for mode, kwargs, ref in CASES:
        show(mode, kwargs, ref)

    print()
    print("=" * 78)
    print("  Alla lagen: bit 8 (Forcecharge Peakshaving) AV, reservgolv 15 %.")
    print("  Endast laddlage ror charge_limit_power och TOU-fonstret.")
    print("=" * 78)
    print()


if __name__ == "__main__":
    main()
