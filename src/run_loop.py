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

    # Beräkna laddström (A) från nätet
    effective_grid_charge_kw = min(10.0, next_grid_charge_kw)
    if effective_grid_charge_kw > 0.05:
        target_charge_amps = round((effective_grid_charge_kw * 1000.0) / battery_voltage, 1)
        target_charge_amps = max(1.0, min(40.0, target_charge_amps))
    else:
        target_charge_amps = 0.0

    # --- backup_soc och bit 5: ETT register med TVA funktioner -----------------
    #
    # Register 43024 (backup_soc) ar inte en sparr utan ett BORVARDE som verkar
    # i bada riktningarna (SPEC-effektregulator.md 2.4, kausalt matt 2026-11-19):
    #
    #   golv UNDER aktuell SOC -> Self-Use laddar ur ned mot golvet
    #   golv OVER  aktuell SOC -> natladdning upp till golvet, fran natet, om
    #                             bit 5 (allow_grid_to_charge_the_battery) ar pa
    #
    # SOC-sensorn (input-register 33139) har 1 %-upplosning: 1326 matpunkter
    # 14-21 nov 2026 inneholl inte ett enda decimalvarde. round(), floor() och
    # int() ger darfor IDENTISKT resultat - att byta till floor() vore en no-op.
    #
    # Darav buggen som lag i drift till 2026-11-21. Koden skrev, i vantelage,
    # golv = round(SOC) for att "lasa" batteriet. Eftersom visat SOC ar en ovre
    # grans for det verkliga vardet hamnade golvet pa eller over det verkliga,
    # och varje kvart blev darfor en natladdningsorder: reservvagen drog full
    # effekt (43012 = 50,0 A) tills SOC tickat ett helt steg.
    #
    # Matt 2026-11-21: 37 burstar, 93 % av kvartarna 03:00-10:00, 0,19-0,30 kWh
    # styck = 0,75-1,19 % av batteriet, alla 5-16 s efter appens skrivning.
    # Batteriet laddades 12 -> 48 % utan att planen nagon gang begarde
    # natladdning, och 44 burstar mot 0-7 per dag veckan innan.
    #
    # SLUTSATSEN: med bit 5 pa finns INGET varde pa 43024 som betyder "hall".
    #   golv > SOC -> laddar, golv < SOC -> laddar ur, golv = SOC -> omojligt att
    #   traffa med 1 % upplosning (och hamnar i praktiken over).
    # En hall-funktion maste darfor uttryckas i BIT 5, inte i golvet.
    #
    # Vi delar upp rollerna:
    #   bit 5  = far batteriet laddas fran natet overhuvudtaget?
    #   43024  = urladdningsgolv (och laddmal ENDAST nar bit 5 ar pa)
    #
    # Nattoppladdning sker via TOU slot 1, som har egen cut-off (43708) och egen
    # stromsalva - den vagen ar effektreglerad. Reservvagen ar det INTE (den gar
    # pa 43012 = 50 A). Darfor ska golvet hallas lagt sa lange vi laddar: annars
    # tander reservvagen och tar over med okontrollerad effekt.
    want_grid_charge = next_grid_charge_kw > 0.05
    want_lock = not (want_grid_charge or is_tariff_window or next_discharge_kw > 0.05)

    # Golvvardet som ALLTID ar ofarligt: under alla tankbara SOC-varden (utom
    # under 15 %, dar overdischarge_soc = 14 ar det harda skyddet).
    # Ett golv pa SAFE_FLOOR kan aldrig bli en natladdningsorder.
    SAFE_FLOOR = 15

    target_backup_soc = int(min(100, max(SAFE_FLOOR, int(current_soc)))) if want_lock else SAFE_FLOOR

    # Bit 5: pa ENDAST nar planen faktiskt vill ladda fran natet. I alla andra
    # lagen ar den av, sa att en hogning av golvet aldrig kan bli en order.
    # Skrivs bara vid andring - 43110 har ~40 s skrivlatens och ska inte roras
    # i onodan. Switch-entiteten skoter bitthanteringen i integrationen, sa vi
    # ror aldrig 43110 rakt (bit 1/TOU far inte skrivas over).
    want_grid_charge_permission = want_grid_charge

    log(f"Optimerat börvärde för kommande kvart (v2): Ladda={next_charge_kw:.2f} kW (Nät={next_grid_charge_kw:.2f} kW -> {target_charge_amps} A vid {battery_voltage:.1f} V), Urladda={next_discharge_kw:.2f} kW, Target Backup SOC={target_backup_soc}%, Nuvarande SOC={current_soc:.1f}%")

    if not is_active:
        status_text = "Inaktiv (batterycontrol_active = off)"
        log(f"Styrning: {status_text}. Inga ändringar utförs.")
    elif is_dry_run:
        status_text = "Skuggläge / Dry Run (v2)"
        log(f"Styrning: {status_text}.")
        log(f"[DRY RUN SIMULERING v2] Skulle ha skickat: TOU Slot 1 Ström={target_charge_amps} A (Nätladdning: {next_grid_charge_kw:.2f} kW), Urladda={next_discharge_kw:.2f} kW, Backup SOC={target_backup_soc}% (nuvarande: {current_backup_soc}%)")
    else:
        status_text = "Aktiv styrning (Live Modbus v2)"
        log(f"Styrning: {status_text}.")

        # SEKVENSUPPBYGGNAD - latenserna ar OMVANDA, sa "ratt ordning" racker inte.
        #
        # Uppmatt skrivlatens: 43110 (bit 5 via switch) upp till ~40 s,
        # 43024 (backup_soc) ~5 s. Skulle vi stanga bit 5 forst och golvet sedan
        # landar golvet ~35 s FORRE bit 5, och under den tiden star ett hogt golv
        # medan natladdning fortfarande ar tillaten - precis det fonster som gav
        # 8-10 kW i de gamla fallen (SPEC 2.4).
        #
        # Losningen ar inte att ordna tva asynkrona skrivningar, utan att aldrig
        # skriva ett farligt varde: nar vi gar in i "las" skriver vi forst golvet
        # till SAFE_FLOOR (15), som ar ofarligt aven med bit 5 pa. Forst darefter
        # satts det riktiga golvet, och bit 5 stangs. Da finns inget fonster dar
        # hogt golv och tillaten natladdning sammanfaller - oavsett latens.
        #
        # Vill vi ladda: oppna bit 5 forst (annars nekar invertern TOU-laddningen),
        # och golvet ar 15 hela tiden - lagt och ofarligt anda.
        bit5_state = client.get_state("switch.allow_grid_to_charge_the_battery")
        bit5_on = bool(bit5_state and bit5_state.get("state") == "on")

        if want_lock:
            # Steg 1: farligt golv bort forst. Skrivs alltid nar kranen ar oppen -
            # vi kan inte lita pa current_backup_soc (den defaultar till 15 vid
            # lasfel, vilket skulle dolja ett hogt golv). Ett golv pa SAFE_FLOOR
            # ar ofarligt aven med bit 5 pa, sa skrivningen ar idempotent och
            # kostar inget utom ~5 s latens.
            wrote_safe = False
            if bit5_on or int(current_backup_soc) > SAFE_FLOOR:
                log(f"[LIVE MODBUS] Sänker backup_soc till säkert golv {SAFE_FLOOR}% innan bit 5 stängs (var {current_backup_soc}%)")
                client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_backup_soc", "value": SAFE_FLOOR})
                wrote_safe = True
            # Steg 2: stang kranen.
            if bit5_on:
                log("[LIVE MODBUS] Stänger bit 5 (allow_grid_to_charge_the_battery): ingen nätladdning planerad")
                client.call_service("switch", "turn_off", {"entity_id": "switch.allow_grid_to_charge_the_battery"})
            # Steg 3: nu ar ett hogt golv ofarligt - det kan bara strypa urladdning.
            # Skrev steg 1, star registret pa SAFE_FLOOR oavsett current_backup_soc,
            # sa malvardet maste skrivas aven om det rakar lika det gamla vardet.
            if wrote_safe or int(current_backup_soc) != target_backup_soc:
                log(f"[LIVE MODBUS] Ändrar number.solis_s6_eh1p_backup_soc: {current_backup_soc}% -> {target_backup_soc}%")
                client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_backup_soc", "value": target_backup_soc})
        else:
            # Ladd- eller urladdningslage: golvet ar redan SAFE_FLOOR.
            if want_grid_charge_permission and not bit5_on:
                log("[LIVE MODBUS] Öppnar bit 5 (allow_grid_to_charge_the_battery): planen vill nätladda")
                client.call_service("switch", "turn_on", {"entity_id": "switch.allow_grid_to_charge_the_battery"})
            if int(current_backup_soc) != target_backup_soc:
                log(f"[LIVE MODBUS] Ändrar number.solis_s6_eh1p_backup_soc: {current_backup_soc}% -> {target_backup_soc}%")
                client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_backup_soc", "value": target_backup_soc})
            if not want_grid_charge_permission and bit5_on:
                log("[LIVE MODBUS] Stänger bit 5 (allow_grid_to_charge_the_battery): urladdningsläge, ingen nätladdning")
                client.call_service("switch", "turn_off", {"entity_id": "switch.allow_grid_to_charge_the_battery"})

        # TOU Slot 1 Nätladdning:
        plan_48h = plan_attrs.get("plan_48h", [])
        if next_grid_charge_kw > 0.05:
            now_dt = datetime.datetime.now(sthlm_tz)
            start_minute = (now_dt.minute // 15) * 15
            start_str = f"{now_dt.hour:02d}:{start_minute:02d}:00"
            
            charge_consecutive_quarters = 0
            for p in plan_48h:
                if p.get("grid_charge_kw", p.get("charge_kw", 0.0)) > 0.05:
                    charge_consecutive_quarters += 1
                else:
                    break
            if charge_consecutive_quarters == 0:
                charge_consecutive_quarters = 1
                
            end_dt = now_dt.replace(minute=start_minute, second=0, microsecond=0) + datetime.timedelta(minutes=15 * charge_consecutive_quarters)
            end_str = f"{end_dt.hour:02d}:{end_dt.minute:02d}:00"

            log(f"[LIVE MODBUS] Aktiverar TOU Slot 1 laddning: {start_str} -> {end_str}, Ström={target_charge_amps} A ({next_grid_charge_kw:.2f} kW nät), Cut-off SOC=100%")
            client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_1", "value": 100})
            client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1", "value": target_charge_amps})
            client.call_service("time", "set_value", {"entity_id": "time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_1", "time": start_str})
            client.call_service("time", "set_value", {"entity_id": "time.solis_s6_eh1p_grid_time_of_use_charge_end_slot_1", "time": end_str})
            client.call_service("switch", "turn_on", {"entity_id": "switch.grid_time_of_use_charging_period_1"})
        else:
            slot1_sw = client.get_state("switch.grid_time_of_use_charging_period_1")
            if slot1_sw and slot1_sw.get("state") == "on":
                log("[LIVE MODBUS] Ingen nätladdning nu: stänger switch.grid_time_of_use_charging_period_1")
                client.call_service("switch", "turn_off", {"entity_id": "switch.grid_time_of_use_charging_period_1"})
            client.call_service("number", "set_value", {"entity_id": "number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1", "value": 0.0})

        log(f"[LIVE MODBUS] Tillämpat: TOU Slot 1 Ström={target_charge_amps} A (Nät={next_grid_charge_kw:.2f} kW), Urladda={next_discharge_kw:.2f} kW, Backup SOC={target_backup_soc}%")

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
            "target_soc_percent": target_soc,
            "target_backup_soc": target_backup_soc,
            "current_soc": current_soc,
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
