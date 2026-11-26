import sys, os, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.optimizer_v2 import build_quarter_plan_attributes

def mk(i, base="2026-11-26T12:00:00+01:00"):
    dt = datetime.datetime.fromisoformat(base) + datetime.timedelta(minutes=15*i)
    return {
        "time": dt.isoformat(), "time_str": dt.strftime("%a %H:%M"),
        "hour": dt.hour, "minute": dt.minute,
        "load_kw": 2.44, "pv_kw": 3.28, "ev_grid_kw": 0.0,
        "grid_import_kw": 0.0, "grid_charge_kw": 0.0, "discharge_kw": 0.0,
        "grid_export_kw": 0.0,
        "soc_kwh": 19.0, "soc_pct": 76.0, "soc_end_kwh": 19.2, "soc_end_pct": 76.8,
        "price_in": 5.157, "price_out": 3.7, "tariff_threshold_kw": 4.34,
        "datetime": dt.isoformat(), "charge_kw": 0.0, "soc_percent": 76.0,
    }

ok = True
def chk(cond, msg):
    global ok
    print(("  OK   " if cond else "  FEL  ") + msg)
    if not cond: ok = False

print("=== 1. tom plan ===")
s, a = build_quarter_plan_attributes([])
chk(s == 0.0, "state 0.0 vid tom plan")
chk(a["plan"] == {}, "tomt plan-attribut")

print("\n=== 2. 192 kvartar (48h) ===")
plan = [mk(i) for i in range(192)]
s, a = build_quarter_plan_attributes(plan, soc_now_pct=76.0, t_threshold=4.34,
        peak_info={"p1":6.31,"p2":6.05,"p3":4.34,"p_max_today":0.64})
chk(a["quarters"] == 192, f"quarters=192 (fick {a['quarters']})")
chk(a["step_minutes"] == 15, "step_minutes=15")
chk(a["start"] == plan[0]["time"], "start = forsta kvartens ISO")
chk(len(a["plan"]["time"]) == 192, "time[] har 192 poster")
chk(a["plan"]["time"][0] == "12:00", f"forsta tid HH:MM (fick {a['plan']['time'][0]})")
chk(a["plan"]["time"][1] == "12:15", "andra tid 12:15")
chk(s == 5.157, f"state = forsta kvartens price_in (fick {s})")
chk(a["p1_kw"] == 6.31 and a["p3_kw"] == 4.34, "P1/P3 pa toppniva")
chk(a["soc_now"] == 76.0, "soc_now pa toppniva")

print("\n=== 3. STORLEK (recorder-grans 16384 B) ===")
b = len(json.dumps({**a}, separators=(',',':'), ensure_ascii=False).encode())
chk(b < 16384, f"hela attributpaketet {b} B < 16384 B")
print(f"       marginal: {16384-b} B ({100*(16384-b)/16384:.0f}%)")

print("\n=== 4. tidssteg ar strikt 15 min (konsument far rakna sjalv) ===")
dts = [datetime.datetime.fromisoformat(plan[i]["time"]) for i in range(192)]
steps = {int((dts[i+1]-dts[i]).total_seconds()) for i in range(191)}
chk(steps == {900}, f"alla steg 900 s (fick {steps})")

print("\n=== 5. diagram-underlag gar att rakna fram ===")
time_, house, pv = a["plan"]["time"], a["plan"]["house"], a["plan"]["pv"]
netto = [round(h-p,2) for h,p in zip(house,pv)]
chk(netto[0] == -0.84, f"graf1 netto = house-pv (fick {netto[0]})")
battnet = [round(d-c,2) for d,c in zip(a["plan"]["batt_discharge"], a["plan"]["batt_charge"])]
chk(len(battnet) == 192, "graf2 battNet gar att rakna")

print("\n=== 6. saknat tariff_threshold -> None, inte krasch ===")
p2 = [mk(0)]; p2[0]["tariff_threshold_kw"] = None
s2, a2 = build_quarter_plan_attributes(p2)
chk(a2["plan"]["threshold"][0] is None, "None bevaras")

print("\n" + ("ALLA TEST OK" if ok else "TEST MISSLYCKADES"))
sys.exit(0 if ok else 1)
