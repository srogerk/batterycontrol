#!/usr/bin/env python3
"""Validerar ENTITY-kartan mot den levande HA-instansen - utan att skriva.

For varje entitet i battery_control.ENTITY:
  * finns den?
  * ar den inte 'unavailable'?
  * ligger varje varde build_actions() vill skriva inom min/max?

Kor: python3 tests/validate_entities.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/tmp")

from src import battery_control as bc  # noqa: E402

import ha  # noqa: E402  - /tmp/ha.py


def build_wanted():
    """entity_id -> [(varde, lage), ...] for alla lagen."""
    wanted = {}
    for mode in bc.ALL_MODES:
        for a in bc.build_actions(
            mode, charge_kw=8.0, discharge_kw=4.0, grid_import_kw=2.0
        ):
            ent = a["data"]["entity_id"]
            if a["service"] in ("turn_on", "turn_off"):
                val = 1 if a["service"] == "turn_on" else 0
            else:
                val = a["data"].get("value")
                if val is None:
                    continue  # tidsstangder ar inte numeriska
            wanted.setdefault(ent, []).append((val, mode))
    for slot, ent in bc.OTHER_TOU_CHARGE_SLOTS.items():
        wanted.setdefault(ent, []).append((0, "all"))
    return wanted


def main():
    ha.connect()
    states = ha.states()
    wanted = build_wanted()

    problems = []
    print()
    print("=" * 78)
    print("  ENTITETSVALIDERING mot levande HA")
    print("=" * 78)

    entities = dict(bc.ENTITY)
    for slot, ent in bc.OTHER_TOU_CHARGE_SLOTS.items():
        entities[f"tou{slot}_switch_off"] = ent

    for key, ent in sorted(entities.items(), key=lambda kv: kv[1]):
        st = states.get(ent)
        if st is None:
            problems.append(f"SAKNAS: {ent} ({key})")
            print(f"  [SAKNAS ] {ent}")
            continue

        state = st.get("state")
        attrs = st.get("attributes") or {}
        flags = []
        if state in ("unavailable", "unknown"):
            problems.append(f"OTILLGANGLIG: {ent} = {state}")
            flags.append(f"state={state}")

        vals = wanted.get(ent, [])
        numeric = [(v, m) for v, m in vals if isinstance(v, (int, float))]
        if numeric:
            lo, hi = attrs.get("min"), attrs.get("max")
            for v, mode in numeric:
                if lo is not None and v < lo:
                    problems.append(f"UNDER MIN: {ent} {v} < {lo} ({mode})")
                    flags.append(f"{mode}:{v}<min{lo}")
                if hi is not None and v > hi:
                    problems.append(f"OVER MAX: {ent} {v} > {hi} ({mode})")
                    flags.append(f"{mode}:{v}>max{hi}")

        mark = "OK      " if not flags else "PROBLEM "
        rng = ""
        if "min" in attrs or "max" in attrs:
            rng = f"  [{attrs.get('min')}..{attrs.get('max')}]"
        vals_txt = sorted({v for v, _ in vals}) if vals else []
        print(f"  [{mark}] {ent:66s} = {str(state)[:14]:16s}{rng}")
        if flags:
            print(f"             -> {'; '.join(flags)}")
        elif vals_txt:
            print(f"             skrivs: {vals_txt}")

    print()
    print("=" * 78)
    if problems:
        print(f"  {len(problems)} PROBLEM:")
        for p in problems:
            print(f"    - {p}")
        ha.close()
        return 1
    print(f"  Alla {len(entities)} entiteter finns, ar tillgangliga och tar emot")
    print("  de varden koden vill skriva.")
    print("=" * 78)
    print()
    ha.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
