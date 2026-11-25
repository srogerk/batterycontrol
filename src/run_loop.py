#!/usr/bin/env python3
"""
Styrloop för batteristyrning (körs varje kvart via cron).
Använder Optimizer v2 (HiGHS LP, 25 kWh nominell, mjuk laddning, effekttariff P_tak, skyddad elbilsladdning).

Backup av v1 finns i: src/run_loop_v1.py och src/optimizer.py.
"""

import sys
import os
import json
import time
import datetime
import zoneinfo

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.forecast as forecast_module
import src.optimizer_v2 as optimizer_module
import src.generate_plot as plot_module
import src.generate_web as web_module
import src.battery_control as bc

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batterycontrol.log")

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
    log("=== Startar kvartsoptimering (Optimizer v2) ===")
    # Logga vilka ytor som faktiskt anvands. I add-on-lage ar skillnaden
    # mellan /data och /app/web hela skillnaden mellan bestandigt och borta
    # vid nasta omstart - och den framgar inte av nagon annan loggrad.
    try:
        import src.paths as _paths
        log(f"Ytor: data={_paths.data_dir()} config={_paths.config_dir()}")
    except Exception as _e:
        log(f"FEL: kunde inte avgora data/config-katalog: {_e}")
        sys.exit(1)
    
    # 1. Prognos
    try:
        log("Steg 1: Uppdaterar adaptiv last- och solprognos...")
        forecast_module.main()
    except Exception as e:
        log(f"FEL i prognosmodul: {e}")
        sys.exit(1)

    # 2. Optimering v2
    #
    # VIKTIGT: main() returnerar False när optimeringen misslyckades. I så fall
    # ligger FÖREGÅENDE kvarts plan kvar i sensor.batterycontrol_plan_now, och
    # stegen nedan läser börvärden ur just det sensorns attribut. Att fortsätta
    # vore att styra på inaktuella data medan det ser ut som att allt fungerar.
    #
    # Så hände 2026-11-12: LP:n blev infeasible kl 16:30, 17:00 och 18:00, men
    # run_loop fortsatte och skrev börvärden (1.05 kW respektive 0.05 kW) som
    # kom från den gamla planen - inte från någon lösning alls.
    #
    # En misslyckad optimering ska vara ett synligt styrstopp, inte en tyst
    # övergång till gammal data.
    try:
        log("Steg 2: Kör HiGHS LP-optimering v2...")
        optimering_ok = optimizer_module.main()
    except Exception as e:
        log(f"FEL i optimeringsmodul: {e}")
        sys.exit(1)

    if not optimering_ok:
        log("AVBRYTER: optimeringen gav ingen plan. Styr INTE på föregående kvarts "
            "börvärden - de är inaktuella. Ingen skrivning till inverteraren görs "
            "denna kvart. Kontrollera LP-status i loggen ovan.")
        sys.exit(2)

    # 3. Kontrollera läge i HA
    client = optimizer_module.HAPriceAndStateClient()
    active_state = client.get_state("input_boolean.batterycontrol_active")
    dry_run_state = client.get_state("input_boolean.batterycontrol_dry_run")
    plan_state = client.get_state("sensor.batterycontrol_plan_now")
    voltage_state = client.get_state("sensor.solis_s6_eh1p_battery_voltage")
    soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")

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

    plan_attrs = plan_state.get("attributes", {}) if plan_state else {}
    next_charge_kw = plan_attrs.get("next_charge_kw", 0.0)
    next_grid_charge_kw = plan_attrs.get("next_grid_charge_kw", next_charge_kw)
    next_pv_charge_kw = plan_attrs.get("next_pv_charge_kw", 0.0)
    next_discharge_kw = plan_attrs.get("next_discharge_kw", 0.0)
    target_soc = plan_attrs.get("target_soc_percent", 0.0)

    # Rädda timmen-kontroll under effekttariff
    sthlm_tz = zoneinfo.ZoneInfo("Europe/Stockholm")
    now_local = datetime.datetime.now(sthlm_tz)
    is_tariff_window = optimizer_module.is_effekttariff_day(now_local) and (7 <= now_local.hour < 20)

    if is_tariff_window and now_local.minute >= 15:
        grid_meter_state = client.get_state("sensor.solis_s6_eh1p_meter_total_active_energy_from_grid")
        if grid_meter_state:
            log(f"[RÄDDA TIMMEN] Aktiv timvakt: klockan är {now_local.strftime('%H:%M')}, nätmätarställning: {grid_meter_state.get('state')} kWh")

    # Laddstrommen beraknas i battery_control.charge_current_a() nedan, sa att
    # samma funktion anvands bade for styrningen och for status-sensorn.
    # (Den gamla berakningen här klamde till 40 A och skrevs anda over.)

    # --- Styrning via batterientiteter (spec 7 a-e, 2026-11-23) --------------
    #
    # Den gamla koden har blandade roller for backup_soc, bit 5 och TOU slot 1,
    # och kunde inte uttrycka "inaktivt batteri" eller "urladdning styrd pa
    # nateffekt". Natten 2026-11-23 gav den 0 W laddning i timmar medan alla
    # register sag korrekta ut - felet satt i en entitet koden aldrig rorde
    # (battery_charge_limit_power = 0) plus en peak-shaving-bit.
    #
    # All styrning ligger nu i src/battery_control.py som en REN funktion:
    # indata -> lista av tjansteanrop. Den ar testad i tests/ utan vaxelriktare.
    #
    # Roller:
    #   battery_control.choose_mode()  valjer av de fem lagen i spec 7
    #   build_actions()                ger exakt vilka entiteter som ska skrivas
    next_grid_import_kw = plan_attrs.get("next_grid_import_kw", 0.0)

    control_mode = bc.choose_mode(
        charge_kw=next_grid_charge_kw,
        discharge_kw=next_discharge_kw,
        is_tariff_window=is_tariff_window,
    )

    actions = bc.build_actions(
        control_mode,
        charge_kw=next_grid_charge_kw,
        discharge_kw=next_discharge_kw,
        grid_import_kw=next_grid_import_kw,
        voltage=battery_voltage,
        now=now_local,
    )

    target_charge_amps = bc.charge_current_a(next_grid_charge_kw, battery_voltage)
    target_backup_soc = bc.SAFE_BACKUP_SOC
    want_grid_charge_permission = (control_mode == bc.MODE_CHARGE)

    log(f"Optimerat börvärde för kommande kvart (v2): Ladda={next_charge_kw:.2f} kW "
        f"(Nät={next_grid_charge_kw:.2f} kW -> {target_charge_amps} A vid "
        f"{battery_voltage:.1f} V), Urladda={next_discharge_kw:.2f} kW, "
        f"Target Backup SOC={target_backup_soc}%, Nuvarande SOC={current_soc:.1f}%")
    log(f"Styrlage: {bc.describe(control_mode)} ({control_mode})"
        f"{' [tariffonster]' if is_tariff_window else ''}")

    if not is_active:
        status_text = "Inaktiv (batterycontrol_active = off)"
        log(f"Styrning: {status_text}. Inga ändringar utförs.")
    elif is_dry_run:
        status_text = "Skuggläge / Dry Run (v2)"
        log(f"Styrning: {status_text}.")
        bc.execute(actions, client, dry_run=True, logger=log)
    else:
        status_text = f"Aktiv styrning ({bc.describe(control_mode)})"
        log(f"Styrning: {status_text}.")
        # Diffa mot nuvarande tillstand: skriv bara det som faktiskt andras.
        # Ordningen och grind-sist-kontraktet ar oforandrat - diffningen
        # filtrerar bort no-ops, den flyttar inget.
        #
        # Verifieringen nedan behover hela listan (aven de skrivningar som
        # diffades bort) - den jamfor mot vad laget AVSAG, inte mot vad som
        # raakade skrivas. Darfor sparas den undan fore diffningen.
        full_actions = actions
        wanted = len(actions)
        actions = bc.diff_actions(actions, client, logger=log)
        log(f"[DIFF] {wanted - len(actions)} av {wanted} skrivningar redan "
            f"korrekta, {len(actions)} att utfora.")
        bc.execute(actions, client, dry_run=False, logger=log)
        log(f"[LIVE MODBUS] Tillämpat: {bc.describe(control_mode)}, "
            f"Urladda={next_discharge_kw:.2f} kW, Backup SOC={target_backup_soc}%")

        # Las tillbaka laddfonstret. Att skriva ratt ar inte samma sak som att
        # enheten har fatt det - natten 2026-11-23 sag allt ratt ut och gav
        # anda 0 W. En kvart ar lang tid att vanta pa att fa veta det.
        if control_mode == bc.MODE_CHARGE:
            time.sleep(2.0)  # ge enheten en chans att verkstalla skrivningarna
            if not bc.verify_charge_window(client, full_actions, logger=log):
                status_text = (f"Aktiv styrning ({bc.describe(control_mode)}) "
                               "- VERIFIERING MISSLYCKADES")
                log("VARNING: laddfonstret kunde inte bekraftas i enheten. "
                    "Kontrollera slot 5 och periodmasken 43707 manuellt innan "
                    "du litar pa att kvarten laddar.")

        # Styrning av elbilsladdning
        ev_order_active = plan_attrs.get("ev_charge_order_active", False)
        ev_charge_now = plan_attrs.get("ev_charge_now", False)
        ca_payload = {"chargepoint": "2009013389M", "connector": 1}

        if ev_order_active:
            if ev_charge_now:
                log("[ELBIL v2] Aktiv laddkvart: sätter maxström till 8 A och aktiverar Charge Amps laddbox")
                client.call_service("chargeamps", "set_max_current", {**ca_payload, "max_current": 8})
                client.call_service("chargeamps", "enable", ca_payload)
            else:
                # Kvarten ligger utanför det bokade blocket (före start eller efter
                # slut) — då ska boxen vara av, annars fortsätter bilen ladda efter
                # blockets slut.
                log("[ELBIL v2] Ingen laddning planerad denna kvart: laddboxen avstängd")
                client.call_service("chargeamps", "disable", ca_payload)
        # Ingen order: rör inte laddboxen. Manual laddning ska fungera ostört.
        #
        # Säkerhetsspärren som låg här ("elif is_tariff_window: chargeamps.disable")
        # togs bort 2026-11-11 på Rogers begäran. Den stängde av boxen oavsett om
        # någon order fanns, och blockerade därmed manuell laddning.

    # Uppdatera status-sensor i HA
    client.post_state(
        "sensor.batterycontrol_status",
        status_text,
        {
            "friendly_name": "Batteristyrning Status",
            "active": is_active,
            "dry_run": is_dry_run,
            "version": "v2",
            "last_run": datetime.datetime.now().isoformat(),
            "next_charge_kw": next_charge_kw,
            "next_grid_charge_kw": next_grid_charge_kw,
            "next_pv_charge_kw": next_pv_charge_kw,
            "next_charge_amps": target_charge_amps,
            "battery_voltage": battery_voltage,
            "next_discharge_kw": next_discharge_kw,
            "next_grid_import_kw": next_grid_import_kw,
            "target_soc_percent": target_soc,
            "target_backup_soc": target_backup_soc,
            "current_soc": current_soc,
            # Styrlaget enligt spec 7 a-e. Synliggor i HA vilken av de fem
            # vaggarna som faktiskt anvands - natten 23 nov kunde felet halla i
            # sig i timmar just for att ingenting visade hur styrningen sag ut.
            "control_mode": control_mode,
            "control_mode_text": bc.describe(control_mode),
            "control_actions": len(actions),
            # Synliggor bit 5-laget. Buggen 2026-11-21 kunde halla i sig i dygn
            # just for att ingenting i HA visade att natladdning var TILLATEN
            # medan planen sade vila. Nu ar laget lasbart i plan-sensorns attribut.
            "grid_charge_allowed": want_grid_charge_permission,
        }
    )

    client.close()

    # Uppdatera visualisering och GUI-dashboard
    try:
        plot_module.generate_plan_chart()
        web_module.main()
        log("Graf och web-dashboard uppdaterade.")
    except Exception as e:
        log(f"Varning: Kunde inte uppdatera web-dashboard: {e}")

    log("=== Kvartsoptimering slutförd (v2) ===")

if __name__ == "__main__":
    main()
