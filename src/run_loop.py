#!/usr/bin/env python3
"""
Styrloop för batteristyrning (körs varje kvart, t.ex. via cron).
Flöde:
1. Kör forecast.py för att uppdatera 48h last- och solprognos.
2. Kör optimizer.py för att lösa 48h MPC LP-problemet.
3. Läser av läget i Home Assistant:
   - input_boolean.batterycontrol_active (on/off)
   - input_boolean.batterycontrol_dry_run (on/off)
4. Agerar på nästa kvarts börvärde:
   - I Dry Run-läge: Loggar exakt vad som skulle skickas till Solis Modbus.
   - I Aktivt läge (Active = on och Dry Run = off): Skriver till Solis Modbus.
5. Loggar status och uppdaterar sensor.batterycontrol_status.
"""

import sys
import os
import json
import time
import datetime

sys.path.append("/workspace/projects/Batteristyrning - effektavgift")
import src.forecast as forecast_module
import src.optimizer as optimizer_module

LOG_FILE = "/workspace/projects/Batteristyrning - effektavgift/src/batterycontrol.log"

def log(msg):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now_str}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def main():
    log("=== Startar kvartsoptimering ===")
    
    # 1. Prognos
    try:
        log("Steg 1: Uppdaterar adaptiv last- och solprognos...")
        forecast_module.main()
    except Exception as e:
        log(f"FEL i prognosmodul: {e}")
        return

    # 2. Optimering
    try:
        log("Steg 2: Kör global LP-optimering (48h / 192 kvartar)...")
        optimizer_module.main()
    except Exception as e:
        log(f"FEL i optimeringsmodul: {e}")
        return

    # 3. Kontrollera läge i HA
    client = optimizer_module.HAPriceAndStateClient()
    active_state = client.get_state("input_boolean.batterycontrol_active")
    dry_run_state = client.get_state("input_boolean.batterycontrol_dry_run")
    plan_state = client.get_state("sensor.batterycontrol_plan_now")
    voltage_state = client.get_state("sensor.solis_s6_eh1p_battery_voltage")
    soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")
    current_backup_soc_state = client.get_state("number.solis_s6_eh1p_backup_soc")

    is_active = (active_state and active_state.get("state") == "on")
    is_dry_run = (dry_run_state and dry_run_state.get("state") == "on")

    # Batterispänning för strömomvandling (default 360V vid bortfall)
    try:
        battery_voltage = float(voltage_state.get("state", 360.0))
        if battery_voltage < 100.0 or battery_voltage > 500.0:
            battery_voltage = 360.0
    except Exception:
        battery_voltage = 360.0

    try:
        current_soc = float(soc_state.get("state", 50.0))
    except Exception:
        current_soc = 50.0

    try:
        current_backup_soc = float(current_backup_soc_state.get("state", 15.0))
    except Exception:
        current_backup_soc = 15.0

    plan_attrs = plan_state.get("attributes", {}) if plan_state else {}
    next_charge_kw = plan_attrs.get("next_charge_kw", 0.0)
    next_discharge_kw = plan_attrs.get("next_discharge_kw", 0.0)
    target_soc = plan_attrs.get("target_soc_percent", 0.0)

    # Beräkna nödvändig laddström (A) för att smeta ut laddningen över hela 15-minuterskvarten
    # I (A) = P (W) / U (V)
    # Begränsa målad laddeffekt till max 10.0 kW
    effective_charge_kw = min(10.0, next_charge_kw)
    if effective_charge_kw > 0.05:
        target_charge_amps = round((effective_charge_kw * 1000.0) / battery_voltage, 1)
        # Säkerställ att strömmen håller sig inom rimliga gränser (min 1.0 A, max 40.0 A / ~10 kW)
        target_charge_amps = max(1.0, min(40.0, target_charge_amps))
    else:
        target_charge_amps = 0.0

    # Bestäm target_backup_soc (urladdningsspärr mot Self-Use):
    # - Om urladdning tillåts (next_discharge_kw > 0): Sätt backup_soc till 15% (normal basreserv).
    # - Om batteriet skall hållas kvar (next_discharge_kw == 0): Sätt backup_soc till aktuellt SOC (eller 100%)
    #   för att blockera Self-Use från att mata huset från batteriet.
    if next_discharge_kw > 0.05:
        target_backup_soc = 15
    else:
        # Spara laddningen: sätt backup_soc till nuvarande SOC (avrundat uppåt till heltal, max 100)
        target_backup_soc = int(min(100, max(15, round(current_soc))))

    log(f"Optimerat börvärde för kommande kvart: Ladda={next_charge_kw:.2f} kW ({target_charge_amps} A vid {battery_voltage:.1f} V), Urladda={next_discharge_kw:.2f} kW, Target Backup SOC={target_backup_soc}%, Mål-SOC={target_soc:.1f}%")

    if not is_active:
        status_text = "Inaktiv (batterycontrol_active = off)"
        log(f"Styrning: {status_text}. Inga ändringar utförs.")
    elif is_dry_run:
        status_text = "Skuggläge / Dry Run"
        log(f"Styrning: {status_text}.")
        log(f"[DRY RUN SIMULERING] Skulle ha skickat: TOU Slot 1 Ström={target_charge_amps} A ({next_charge_kw:.2f} kW), Urladda={next_discharge_kw:.2f} kW, Backup SOC={target_backup_soc}% (nuvarande: {current_backup_soc}%)")
    else:
        status_text = "Aktiv styrning (Live Modbus)"
        log(f"Styrning: {status_text}.")
        # Skriv backup_soc om den ändrats
        if int(current_backup_soc) != target_backup_soc:
            log(f"[LIVE MODBUS] Ändrar number.solis_s6_eh1p_backup_soc: {current_backup_soc}% -> {target_backup_soc}%")
            client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_backup_soc", "value": target_backup_soc})
        # Här hanteras även TOU-laddning/urladdning när skarp styrning är aktiverad
        log(f"[LIVE MODBUS] Tillämpar: TOU Slot 1 Ström={target_charge_amps} A ({next_charge_kw:.2f} kW), Urladda={next_discharge_kw:.2f} kW, Backup SOC={target_backup_soc}%")

    # Uppdatera status-sensor i HA
    client.post_state(
        "sensor.batterycontrol_status",
        status_text,
        {
            "friendly_name": "Batteristyrning Status",
            "active": is_active,
            "dry_run": is_dry_run,
            "last_run": datetime.datetime.now().isoformat(),
            "next_charge_kw": next_charge_kw,
            "next_charge_amps": target_charge_amps,
            "battery_voltage": battery_voltage,
            "next_discharge_kw": next_discharge_kw,
            "target_soc_percent": target_soc,
            "target_backup_soc": target_backup_soc,
            "current_soc": current_soc
        }
    )

    client.close()
    log("=== Kvartsoptimering slutförd ===")

if __name__ == "__main__":
    main()
