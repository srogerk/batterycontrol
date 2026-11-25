"""Styrning av Solis-vaxelriktaren via Home-Assistant-entiteter.

Kalla: Rogers spec 2026-11-23, avsnitt 7 (a-e), samt SPEC-effektregulator.md.

VIKTIGT - varfor denna modul finns
----------------------------------
`run_loop.py` styrde tidigare med en blandning av TOU slot 1, backup_soc och
bit 5, och kunde inte uttrycka "inaktivt batteri" eller "urladdning styrd pa
nateffekt". Natten 2026-11-23 gav det 0 W laddning i timmar medan alla register
sag korrekta ut; det verkliga felet satt i en entitet koden aldrig rorde
(`battery_charge_limit_power` = 0) plus en peak-shaving-bit som strypte
laddningen till noll.

Denna modul gor tva saker som run_loop inte gjorde:

  1. Varje styrlage ar en komplett, namngiven uppsattning entitetsskrivningar -
     inte en blandning som beror av vilka kvartar som rakar sammanfalla.
  2. `build_actions()` ar en REN funktion: indata -> lista av tjansteanrop.
     Den ror ingen I/O, vilket gor varje lage testbart utan vaxelriktare.

Skrivningar sker ALDRIG mot Modbus-register direkt. Bara entiteter.
"""

from __future__ import annotations

import datetime
import json

# --- Styrmoden (spec 7 a-e) -------------------------------------------------

MODE_CHARGE = "charge"                        # a) uppladdning
MODE_DISCHARGE_BATTERY = "discharge_battery"  # b) urladdning, batterieffekt
MODE_DISCHARGE_GRID = "discharge_grid"        # c) urladdning, nateffekt (peak shaving)
MODE_IDLE = "idle"                            # d) inaktivt batteri
MODE_IDLE_GRID = "idle_grid"                  # e) inaktivt, nateffekt (peak shaving)

ALL_MODES = (
    MODE_CHARGE,
    MODE_DISCHARGE_BATTERY,
    MODE_DISCHARGE_GRID,
    MODE_IDLE,
    MODE_IDLE_GRID,
)

# --- Entitetsyta (verifierad 2026-11-23 01:41) -------------------------------

ENTITY = {
    # lages-switchar (bitniva i 43110, undvik select.work_mode som rensar bit 1)
    "self_use":              "switch.self_use_mode",
    "peak_shaving":          "switch.peak_shaving_mode",
    # 43483 bit 7. Tva olika brytare for samma ord ("peak shaving"):
    #   - `peak_shaving_mode` (43110 bit 11) = ARBETSLAGET.
    #   - `grid_peak_shaving_power_enable` (43483 bit 7) = SJA SJA SPARREN som
    #     far 43488 att galla.
    #
    # Empiriskt (2026-11-25, ur Rogers egna takandringar det senaste dygnet):
    #   bit 7 AV + tak 6000 W  -> natinport 8041 W, 69,8 % over taket
    #   bit 7 AV + tak 2000 W  -> natinport 7552 W, aven med bit 11 PA
    #   bit 7 PA + tak 1800 W  -> p95 1676 W, 0,7 % over
    #   bit 7 PA + tak 5000 W  -> max 4727 W, 0 % over
    #
    # Alltsa: 43488 ar verkningslost utan bit 7, och bit 11 ar inte det som
    # haller taket. Att bara skriva taket (som koden gjorde) ar en tyst no-op.
    "grid_peak_shaving":     "switch.grid_peak_shaving_power_enable",
    "forcecharge_peakshav":  "switch.battery_forcecharge_peakshaving",
    "allow_grid_charge":     "switch.allow_grid_to_charge_the_battery",
    # effektgranser
    "charge_limit_power":    "number.solis_s6_eh1p_battery_charge_limit_power",
    "discharge_limit_power": "number.solis_s6_eh1p_battery_discharge_limit_power",
    "max_charge_soc":        "number.solis_s6_eh1p_max_charge_soc",
    "backup_soc":            "number.solis_s6_eh1p_backup_soc",
    # peak shaving
    "peak_max_grid":         "number.solis_s6_eh1p_peak_max_usable_grid_power",
    "peak_baseline_soc":     "number.solis_s6_eh1p_peak_baseline_soc",
    # TOU slot 5 (laddvagen enligt spec 7a)
    "tou5_current":          "number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_5",
    "tou5_cutoff_soc":       "number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_5",
    "tou5_start":            "time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_5",
    "tou5_end":              "time.solis_s6_eh1p_grid_time_of_use_charge_end_slot_5",
    "tou5_switch":           "switch.grid_time_of_use_charging_period_5",
}

# De TOU-laddperioder appen INTE anvander. De maste vara avstaengda, annars kan
# tva fonster vara aktiva samtidigt och inverterns val mellan dem ar odefinierat
# (i praktiken brukar den ta den forst matchande, vilket ger helt fel effekt).
#
# Konkret fall 2026-11-24 23:16: slot 1 stod PA med 02:00-23:00 och 20 A -
# alltsa nastan hela dygnet - medan planen sade 4,0 kW. Utan denna sparr hade
# appen satt slot 5 (15,4 A) ocksa, och utfallet blivit slumpmassigt.
OTHER_TOU_CHARGE_SLOTS = {
    1: "switch.grid_time_of_use_charging_period_1",
    2: "switch.grid_time_of_use_charging_period_2",
    3: "switch.grid_time_of_use_charging_period_3",
    4: "switch.grid_time_of_use_charging_period_4",
    6: "switch.grid_time_of_use_charging_period_6",
}

# --- Konstanter --------------------------------------------------------------

# Spec 7a: "Battery Charge Limit Power: 10000W (Begransar eller hindrar
# laddning om den ar lag eller 0)." Faltet ar det som stal hela natten 23 nov.
CHARGE_LIMIT_W = 10000

# Spec 7a: Cutoff SOC = 100 % (anvands inte for styrning av laddningen).
CHARGE_CUTOFF_SOC = 100
MAX_CHARGE_SOC = 100

# Spec 7b: Backup SOC = 15 %, lagsta varde. Anvands som golv i ALLA icke-
# laddande lagen - aven dar spec inte namner det - sa att en kvarts glapp
# aldrig kan dra ur batteriet under reservnivan.
SAFE_BACKUP_SOC = 15

# Spec 7e: inaktivt batteri via nateffekt = 14 kW. Avtalstak pa timmedel.
PEAK_IDLE_GRID_W = 14000

# Register 43488 har max 15000 i integrationen (hybrid_sensors).
PEAK_GRID_MAX_W = 15000

# Spec 7c: urladdning styrd pa nateffekt kraver att batteriet FAR ladda ur.
# Vi satter en hog explicit grans i stallet for att lita pa att 0 = "obegransad"
# (matning 2026-11-23 00:56: discharge_limit_power = 0 OCH 907 W urladdning -
# 0 blockerar alltsa INTE urladdning, till skillnad fran charge_limit_power).
DISCHARGE_LIMIT_ACTIVE_W = 10000

# Spec 7d/7e vill ha batteriet INAKTIVT och anger darfor 0. Det fungerar inte:
# 0 ar ogiltigt i enheten och ger i stallet FRI urladdning.
#
# Skarpmatt 2026-11-25 01:19-01:41 pa huslast ~2 950 W, importtaket av, SOC 29-32 %:
#
#   entitet   register   faktisk urladdning   utfall
#   10000 W     1000          3 069 W         fri (referens)
#    1000 W      100          1 045 W         taket biter exakt
#     500 W       50            550 W         taket biter
#     200 W       20            226 W         taket biter  <-- lagsta som fungerar
#     100 W       10          3 004 W         biter INTE (specens minimigrans!)
#      50 W        5            -            AVVISAS, enheten behaller forra
#       0 W        0          3 064 W         fri
#
# Tva fallgropar:
#  1. ENTITETEN RAKNAR WATT, REGISTRET TIOTAL WATT (faktor 10). Entiteten 50 W
#     skrivs som register 5 och ligger da UNDER enhetens minimi -> enheten
#     avvisar tyst och behaller sitt gamla varde. Det syns i HA:s loggbok som en
#     andring med `user=None` - ingen anropare, bara integrationens avlasning av
#     enhetens faktiska varde. Entitetens `min=0, max=655350` ar REGISTERGRANSER,
#     inte enhetens verkliga granser; den slapper igenom 0 och 50 utan att klaga.
#  2. 100 W (register 10) ar precis pa specens angivna minimi och biter anda
#     INTE. Gransen gar alltsa inte att harleda ur specen - bara ur matning.
#
# 200 W ger 226 W urladdning mot 3 000 W vid 0: en faktor 13. Vald med marginal
# nedat mot det som bevisligen fungerar.
DISCHARGE_LIMIT_IDLE_W = 200

# BMS:ets egen stromgrans (33143/33206 = 50,0 A). Over den nekar enheten.
BMS_MAX_CHARGE_A = 50.0

# Spec 7a sager "slut = kvartens slut + 30 s". Det gar INTE att uttrycka:
# TOU-slotens register 43708..43714 ar sju register med MINUTupplosning och
# ingen sekund - `[cut_off_soc, current*0.1, cut_off_V, start_h, start_min,
# end_h, end_min]`. Last av slot 5 2026-11-24: [100, 312, 0, 10, 45, 12, 0].
#
# Darfor: en hel minut. Det ar den minsta marginal som gar att uttrycka, och
# den ger samma skydd - ar loopen sen behover den inte klippa laddningen mitt i
# en kvart. Overhoppet in i nasta kvart ar ofarligt: nasta varv skriver om
# fonstret, och ett fonster som loper ut stanger sig sjalvt (fallet 2026-11-24:
# switch ON men sluttid 23:00 passerad -> 39 W natinport, ingen laddning).
END_MARGIN_MINUTES = 1

QUARTER_MINUTES = 15

DEFAULT_VOLTAGE = 360.0
VOLTAGE_MIN = 100.0
VOLTAGE_MAX = 500.0

# Troskel for "nagon effekt alls" - samma som run_loop anvander.
EPS_KW = 0.05


# --- Hjalpfunktioner ---------------------------------------------------------


def quarter_window(now: datetime.datetime) -> tuple[str, str]:
    """Returnerar (start, slut) som "HH:MM:SS" for kvarten som `now` ligger i.

    Start = kvartens start. Slut = kvartens slut + 1 min (se
    END_MARGIN_MINUTES for varfor det inte ar 30 s).

    Sekunderna ar alltid 00 - enheten har minutupplosning, och att skicka
    "23:30:30" riskerar att antingen trunkeras eller avrundas uppat beroende
    pa integrationens tolkning. Vi skickar det vi vet att enheten forstar.
    """
    start_minute = (now.minute // QUARTER_MINUTES) * QUARTER_MINUTES
    start = now.replace(minute=start_minute, second=0, microsecond=0)
    end = start + datetime.timedelta(minutes=QUARTER_MINUTES + END_MARGIN_MINUTES)

    # Marginalen far aldrig vicka over midnatt: da blir start > slut (23:45 ->
    # 00:01) och inverterns tolkning av ett sadant fonster ar odefinierad.
    # Klampar vi till 23:59 tappar vi marginalen for just den kvarten - det ar
    # battre an ett fonster enheten inte forstar.
    midnight = start.replace(hour=23, minute=59, second=0, microsecond=0)
    if end > midnight:
        end = midnight
    return start.strftime("%H:%M:%S"), end.strftime("%H:%M:%S")


def sane_voltage(value) -> float:
    """Batterispanning med fallback. Utanfor 100-500 V ar vardet oanvandbart."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_VOLTAGE
    if v < VOLTAGE_MIN or v > VOLTAGE_MAX:
        return DEFAULT_VOLTAGE
    return v


def charge_current_a(charge_kw: float, voltage: float) -> float:
    """Laddeffekt i kW -> strom i A, begransad till BMS:ets 50 A."""
    if charge_kw is None or charge_kw <= EPS_KW:
        return 0.0
    amps = (charge_kw * 1000.0) / sane_voltage(voltage)
    return round(max(0.0, min(BMS_MAX_CHARGE_A, amps)), 1)


def choose_mode(
    *,
    charge_kw: float,
    discharge_kw: float,
    is_tariff_window: bool,
) -> str:
    """Valjer styrlage enligt spec 7.

    - Vill planen ladda -> (a), oavsett tariffonster. Laddning ar en egen
      handling, inte en urladdning med negativt tecken.
    - Vill planen ladda ur: innanfor tariffonstret styrs det pa NATeffekt (c),
      utanfor pa batterieffekt (b). Det ar Rogers uttryckliga onskemal: inom
      tariffonstret ar det natimporterad effekt som kostar pengar.
    - Annars inaktivt: (e) inom tariffonstret, (d) utanfor.

    Tariffonstret avgor alltsa bara HUR urladdning/inaktivitet utforas, aldrig
    OM batteriet laddar.
    """
    if charge_kw is not None and charge_kw > EPS_KW:
        return MODE_CHARGE
    if discharge_kw is not None and discharge_kw > EPS_KW:
        return MODE_DISCHARGE_GRID if is_tariff_window else MODE_DISCHARGE_BATTERY
    return MODE_IDLE_GRID if is_tariff_window else MODE_IDLE


# --- Aktionsbygge (ren funktion, ingen I/O) ---------------------------------


def _act(domain: str, service: str, data: dict, why: str = "") -> dict:
    return {"domain": domain, "service": service, "data": data, "why": why}


def _set_number(entity_key: str, value, why: str = "") -> dict:
    return _act(
        "number",
        "set_value",
        {"entity_id": ENTITY[entity_key], "value": value},
        why,
    )


def _set_switch(entity_key: str, on: bool, why: str = "") -> dict:
    return _act(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": ENTITY[entity_key]},
        why,
    )


def _set_time(entity_key: str, hhmmss: str, why: str = "") -> dict:
    return _act(
        "time",
        "set_value",
        {"entity_id": ENTITY[entity_key], "time": hhmmss},
        why,
    )


def build_actions(
    mode: str,
    *,
    charge_kw: float = 0.0,
    discharge_kw: float = 0.0,
    grid_import_kw: float = 0.0,
    voltage: float = DEFAULT_VOLTAGE,
    now: datetime.datetime | None = None,
) -> list[dict]:
    """Bygger den kompletta listan av tjansteanrop for ett styrlage.

    Ren funktion: inga lasningar, inga skrivningar. Ordningen i listan ar
    den ordning anropen SKA utforas i - den ar en del av kontraktet.

    Ordningsprincip:
      * Laddlage: forutsattningarna (effektgrans, tillstand, lage) skrivs FORE
        fonstret slas pa. En grind som satts pa utan forutsattningar ger
        antingen 0 W (ofarligt) eller okontrollerad effekt - aldrig det senare
        om granserna kommer forst.
      * Alla andra lagen: stang laddfonstret FORST. Att byta lage eller grans
        medan en laddning pagar lamnar en kvarts restvarde kvar i invertern.
    """
    if mode not in ALL_MODES:
        raise ValueError(f"okant styrlage: {mode!r}")
    if now is None:
        now = datetime.datetime.now()

    actions: list[dict] = []

    # Peak-shaving-biten (bit 8) ar den som stal laddningen 2026-11-23: med den
    # pa gav TOU 0 W trots att strom, fonster och grind var korrekta. Den finns
    # inte i spec 7 och ska darfor vara AV i varje lage. Skrivs forst sa att
    # inget efterfoljande steg arver den.
    actions.append(
        _set_switch(
            "forcecharge_peakshav",
            False,
            "bit 8 (Forcecharge Peakshaving) ar av i alla lagen - den strypte "
            "TOU-laddningen till 0 W natten 2026-11-23",
        )
    )

    # Reservgolvet galler i alla lagen. I laddlage ar ett lagt golv dessutom en
    # forutsattning: ar golvet over SOC tenderar reservvagen att ta over med
    # okontrollerad effekt (43012 = 50 A).
    actions.append(
        _set_number(
            "backup_soc",
            SAFE_BACKUP_SOC,
            "reservgolv 15 % - lagsta varde (spec 7b)",
        )
    )

    # De ovriga TOU-laddperioderna maste vara av i ALLA lagen. Tva aktiva
    # fonster ger odefinierad styrning - se OTHER_TOU_CHARGE_SLOTS.
    for slot, ent in sorted(OTHER_TOU_CHARGE_SLOTS.items()):
        actions.append(
            _act(
                "switch",
                "turn_off",
                {"entity_id": ent},
                f"slot {slot} far inte vara aktiv - bara slot 5 anvands",
            )
        )

    if mode == MODE_CHARGE:
        start_str, end_str = quarter_window(now)
        amps = charge_current_a(charge_kw, voltage)

        # 1. Forutsattningar for att laddning ska fungera (spec 7a).
        actions.append(
            _set_number(
                "charge_limit_power",
                CHARGE_LIMIT_W,
                "spec 7a: charge limit power 10000 W - 0 eller lagt varde "
                "hindrar all natladdning",
            )
        )
        actions.append(
            _set_number(
                "max_charge_soc",
                MAX_CHARGE_SOC,
                "spec 7a: max SOC 100 %",
            )
        )
        actions.append(
            _set_switch(
                "peak_shaving",
                False,
                "spec 7a: peak shaving AV vid laddning (bit 11)",
            )
        )
        # Importtaket maste AV vid laddning. Bevis 2026-11-24 22:00-23:00:
        # tak 5000 W lag kvar medan planen bad om 4,0 kW. Huslast 2571 W gav
        # utrymme 2429 W, och laddningen korde 1970 W i stallet for 4000 W -
        # kapat av ett tak ingen kod satt. Taket skiljer inte pa huslast och
        # laddning; det stryper summan.
        actions.append(
            _set_switch(
                "grid_peak_shaving",
                False,
                "spec 7a: importtaket (43483 bit 7) AV - annars kapas "
                "laddningen till (tak - huslast)",
            )
        )
        actions.append(
            _set_switch(
                "self_use",
                True,
                "spec 7a: work mode Self-use",
            )
        )
        actions.append(
            _set_switch(
                "allow_grid_charge",
                True,
                "spec 7a: tillat laddning fran natet = ON (bit 5)",
            )
        )

        # 2. Laddfonstret pa slot 5.
        actions.append(
            _set_number(
                "tou5_cutoff_soc",
                CHARGE_CUTOFF_SOC,
                "spec 7a: cutoff SOC 100 % (styr inte laddningen)",
            )
        )
        actions.append(
            _set_number(
                "tou5_current",
                amps,
                f"spec 7a: laddstrom {amps} A = {charge_kw:.2f} kW vid "
                f"{sane_voltage(voltage):.1f} V",
            )
        )
        actions.append(
            _set_time("tou5_start", start_str, "spec 7a: start = kvartens start")
        )
        actions.append(
            _set_time(
                "tou5_end",
                end_str,
                "spec 7a: slut = kvartens slut + 30 s",
            )
        )
        # Grinden sists - allt ovan ar redan skrivet.
        actions.append(
            _set_switch(
                "tou5_switch",
                True,
                "spec 7a: period 5 ON",
            )
        )
        return actions

    # --- Alla icke-laddande lagen: stang laddfonstret forst -------------------
    actions.append(
        _set_switch("tou5_switch", False, "ingen laddning i detta lage")
    )
    actions.append(
        _set_number("tou5_current", 0.0, "nolla laddstrommen nar fonstret ar av")
    )

    if mode in (MODE_DISCHARGE_BATTERY, MODE_DISCHARGE_GRID, MODE_IDLE, MODE_IDLE_GRID):
        actions.append(
            _set_switch(
                "allow_grid_charge",
                False,
                "ingen natladdning planerad i detta lage (bit 5 av)",
            )
        )

    if mode == MODE_DISCHARGE_BATTERY:
        watts = int(round(max(0.0, discharge_kw) * 1000.0))
        actions.append(
            _set_switch(
                "peak_shaving",
                False,
                "spec 7b: work mode Self-use, ingen peak shaving",
            )
        )
        # Importtaket AV. Det sitter kvar mellan varven och stryper annars
        # urladdningen mot natet - samma mekanism som kapade laddningen.
        actions.append(
            _set_switch(
                "grid_peak_shaving",
                False,
                "spec 7b: importtaket AV - annars kapas urladdningen mot natet",
            )
        )
        actions.append(
            _set_switch("self_use", True, "spec 7b: work mode Self-use")
        )
        actions.append(
            _set_number(
                "discharge_limit_power",
                watts,
                f"spec 7b: urladdningsgrans {watts} W - resten av huslasten "
                "tas fran natet",
            )
        )
        return actions

    if mode == MODE_DISCHARGE_GRID:
        # Nateffektsstyrning: i stallet for att satta en batterigrans satt vi
        # ett TAK pa natimporten. Invertern haller taket genom att ta
        # mellanskillnaden fran batteriet.
        #
        # Taket ar planens egna natimport for kvarten. Det ar samma storhet som
        # balansformeln Phus - Ppv = Pnat + Pbatt loser ut, sa planens
        # grid_import_kw ar redan "hur mycket nat vi vill se".
        # Klampa i kW (registret har 15000 W = 15 kW tak), sedan till watt.
        cap_kw = max(0.0, min(PEAK_GRID_MAX_W / 1000.0, grid_import_kw))
        cap_w = int(round(cap_kw * 1000.0))
        actions.append(
            _set_switch(
                "peak_shaving",
                True,
                "spec 7c: work mode Peak shaving (nateffekt)",
            )
        )
        actions.append(
            _set_number(
                "discharge_limit_power",
                DISCHARGE_LIMIT_ACTIVE_W,
                "spec 7c: batteriet maste fa ladda ur for att kunna halla "
                "nattaket - 0 kan innebara 'obegransat' och ar därfor olampligt "
                "har",
            )
        )
        actions.append(
            _set_number(
                "peak_baseline_soc",
                100,
                "spec 7c: peak baseline SOC 100 % - maste vara >= SOC, annars "
                "tas allt fran batteriet",
            )
        )
        # Sjalva sparren. Utan den ar 43488 verkningslost: 23 nov 21:42-22:08
        # stod taket pa 6000 W med bit 7 AV och importen gick till 8041 W
        # (69,8 % av matpunkterna over taket). Med bit 7 PA holls 1800 W till
        # p95 1676 W hela dagen darpa.
        actions.append(
            _set_switch(
                "grid_peak_shaving",
                True,
                "spec 7c: importtaket (43483 bit 7) PA - utan den ar "
                "peak_max_grid verkningslost",
            )
        )
        actions.append(
            _set_number(
                "peak_max_grid",
                cap_w,
                f"spec 7c: nattak {cap_w} W (planens natimport "
                f"{grid_import_kw:.2f} kW)",
            )
        )
        return actions

    if mode == MODE_IDLE:
        actions.append(
            _set_switch(
                "peak_shaving",
                False,
                "spec 7d: work mode Self-use, ingen peak shaving",
            )
        )
        actions.append(
            _set_switch(
                "grid_peak_shaving",
                False,
                "spec 7d: importtaket AV - batteriet ska vara inaktivt",
            )
        )
        actions.append(
            _set_switch("self_use", True, "spec 7d: work mode Self-use")
        )
        actions.append(
            _set_number(
                "discharge_limit_power",
                DISCHARGE_LIMIT_IDLE_W,
                f"spec 7d: batteriet inaktivt - {DISCHARGE_LIMIT_IDLE_W} W ar "
                "lagsta grans som bevisligen biter (0 ger fri urladdning)",
            )
        )
        return actions

    # MODE_IDLE_GRID
    actions.append(
        _set_switch(
            "peak_shaving",
            True,
            "spec 7e: work mode Peak shaving, batteriet inaktivt",
        )
    )
    actions.append(
        _set_switch(
            "grid_peak_shaving",
            True,
            "spec 7e: importtaket PA - utan den ar peak_max_grid verkningslost",
        )
    )
    actions.append(
        _set_number(
            "discharge_limit_power",
            DISCHARGE_LIMIT_IDLE_W,
            f"spec 7e: batteriet inaktivt - {DISCHARGE_LIMIT_IDLE_W} W ar "
            "lagsta grans som bevisligen biter (0 ger fri urladdning)",
        )
    )
    actions.append(
        _set_number(
            "peak_baseline_soc",
            100,
            "spec 7e: peak baseline SOC 100 %",
        )
    )
    actions.append(
        _set_number(
            "peak_max_grid",
            PEAK_IDLE_GRID_W,
            f"spec 7e: nattak {PEAK_IDLE_GRID_W} W (avtalstak)",
        )
    )
    return actions


# --- Diffning ----------------------------------------------------------------
#
# Varje kvart byggs hela laget (13-17 anrop). Att skriva alla varje gang ar
# sjalvlakande men kostar ~1500 entitetsskrivningar per dygn mot inverterns
# Modbus, och en del av dem ar rena no-ops som bara aldrar enhetens
# flash-minne i onodan.
#
# Diffningen jamfor mot HA:s NUVARANDE tillstand och hoppar over det som redan
# star ratt. Tre regler gor den saker:
#
#   1. `tou5_switch` skrivs ALLTID nar den ska sla PA. Grinden ar
#      bekraftelsen att forutsattningarna ovan ar skrivna; sager HA redan "on"
#      kan fonstret anda ha stangts av enheten (t.ex. vid omstart), och da
#      lamnas kvarten utan laddning om vi hoppar over den. Att sla AV ar
#      daremot en ren no-op om HA redan sager "off" - den diffas normalt.
#   2. Okant tillstand (None/unavailable) raknas som "inte lika" - vi skriver
#      hellre en gang for mycket an hoppar over nagot vi inte kanner.
#   3. Ordningen bevaras. `execute()` ar fortfarande den som kor.
#
# Bieffekt: skrivningarna blir farre, men ordningskontraktet (grind sist i
# laddlage, stang fonstret forst i ovriga) ar oforandrat.

_ALWAYS_WRITE = {"tou5_switch"}


def _coerce(value):
    """Normaliserar ett varde for jamforelse. None om det inte gar."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    s = str(value).strip().lower()
    if s in ("on", "off"):
        return s == "on"
    try:
        return round(float(s), 3)
    except ValueError:
        pass
    # tid: "23:15:00" eller "23:15"
    if len(s) >= 4 and s[2] == ":":
        parts = s.split(":")
        try:
            return tuple(int(p) for p in parts[:2])
        except ValueError:
            return s
    return s


def _current_value(state):
    """Plockar ut det jamforbara vardet ur ett HA-tillstand."""
    if state is None:
        return None
    if isinstance(state, dict):
        if "state" in state:
            return state.get("state")
        return None
    return state


def diff_actions(actions: list[dict], client, logger=print) -> list[dict]:
    """Filtrerar bort aktioner vars malvarde redan ar satt i HA.

    Lasningar sker via `client.get_state(entity_id)`. Misslyckas en lasning
    skrivs aktionen anda - en olasbar entitet far aldrig tysta styrningen.
    """
    kept: list[dict] = []
    for a in actions:
        ent = a["data"].get("entity_id", "")
        want = a["data"].get("value", a["data"].get("time", ""))

        key = next((k for k, v in ENTITY.items() if v == ent), None)
        if key in _ALWAYS_WRITE and a["service"] == "turn_on":
            kept.append(a)
            continue

        try:
            state = client.get_state(ent)
        except Exception as exc:  # olasbar -> skriv
            logger(f"[DIFF] kan inte lasa {ent} ({exc}) - skriver anda")
            kept.append(a)
            continue

        have = _current_value(state)
        if have is None or str(have).lower() in ("unknown", "unavailable"):
            kept.append(a)
            continue

        # service perspektiv: turn_on/turn_off vs set_value
        if a["service"] == "turn_on":
            want_cmp = True
        elif a["service"] == "turn_off":
            want_cmp = False
        else:
            want_cmp = want

        if _coerce(have) != _coerce(want_cmp):
            kept.append(a)
    return kept


# --- Verifiering av laddfonstret ---------------------------------------------
#
# Natten 2026-11-23 sag allt ratt ut pa entitetsniva: strommen stod i slot 5,
# fonstret var satt, grinden var pa - och effekten var anda 0 W. Felet satt i
# ett register koden aldrig rorde (43130) och i en bit som strypte laddningen.
#
# Att skriva ratt ar alltsa inte samma sak som att enheten har fatt det. Den
# har verifieringen laser tillbaka slotens EGNA register efter skrivningen och
# jamfor mot det vi avsag. Den fangar: tyst trunkering, en integration som
# skriver till fel slot, en enhet som sjalv skrivit over vardet, och en grind
# som star pa men utan giltigt fonster.
#
# Lasningen gar via tjansten `solis_modbus.solis_read_register` med
# return_response - den ar en HA-tjanst, inte ett register hack.

TOU_BITMASK_REG = 43707
TOU_SLOT5_FIRST_REG = 43736
# 43483 = "Hybrid Function Control". Bit 7 ar importtaks-sparren som far 43488
# att galla (se kommentaren vid ENTITY["grid_peak_shaving"]). Lasningen behovs
# bara for verifieringen - styrningen sker via switchen, inte registret.
GRID_PEAK_SH_CTRL_REG = 43483
GRID_PEAK_SH_BIT = 7
TOU_SLOT5_BIT = 4          # period 5 -> bit 5-1
TOU_SLOT_STRIDE = 7


def expected_slot5(actions: list[dict]) -> dict | None:
    """Plockar ut de varden vi AVSAG skriva till slot 5 ur aktionslistan.

    Returnerar None om listan inte innehaller ett komplett laddfonster (dvs.
    laget ar inte laddlage) - da finns inget att verifiera.
    """
    want = {}
    for a in actions:
        ent = a["data"].get("entity_id", "")
        for key in ("tou5_cutoff_soc", "tou5_current", "tou5_start", "tou5_end"):
            if ENTITY[key] == ent:
                want[key] = a["data"].get("value", a["data"].get("time"))
    needed = {"tou5_cutoff_soc", "tou5_current", "tou5_start", "tou5_end"}
    return want if needed.issubset(want) else None


def _hhmm(value) -> tuple[int, int] | None:
    """'23:15:00' eller '23:15' -> (23, 15)."""
    try:
        parts = str(value).split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return None


def _register_values(payload) -> list | None:
    """Graver fram `values` ur ett register-svar, oavsett form.

    Formen varierar med integrationsversion och transport: HA:s REST-API ger
    `{"service_response": {"values": [...]}}`, websocket ger en dict, och
    curl-vagen ger en JSON-STRANG av samma sak. Vi letar efter listan i stallet
    for att anta en exakt form - och ger upp efter nagra nivaer hellre an att
    snurra.
    """
    node = payload
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except (ValueError, TypeError):
            return None
    for _ in range(5):
        if isinstance(node, list):
            # en lista av tal ar svaret; en lista av dictar ar nasta niva
            if all(not isinstance(x, dict) for x in node):
                return node
            return None
        if not isinstance(node, dict):
            return None
        for key in ("values", "response", "result", "service_response"):
            if key in node:
                node = node[key]
                break
        else:
            return None
    return None


def verify_charge_window(client, actions: list[dict], logger=print) -> bool:
    """Laser tillbaka slot 5:s register och jamfor mot det vi skrev.

    Returnerar True om enheten har fatt det vi avsag. Kastar aldrig - en
    verifiering som sjalv brakar ar vardelos, och far inte stoppa loopen.
    """
    want = expected_slot5(actions)
    if want is None:
        return True  # inte laddlage, inget fonster att verifiera

    try:
        regs = client.call_service(
            "solis_modbus",
            "solis_read_register",
            {
                "address": TOU_SLOT5_FIRST_REG,
                "register_type": "holding",
                "count": TOU_SLOT_STRIDE,
            },
            return_response=True,
        )
    except Exception as exc:
        logger(f"[VERIFIERING] kunde inte lasa slot 5: {exc}")
        return False

    values = _register_values(regs)
    if not values or len(values) < 5:
        logger(f"[VERIFIERING] olastbart svar fran slot 5: {str(regs)[:180]}")
        return False

    problems = []

    # [0] cutoff SOC, [1] strom x0,1 A, [2] cutoff V, [3] start_h,
    # [4] start_min, [5] end_h, [6] end_min
    got_cutoff = values[0]
    got_amps = round(values[1] / 10.0, 1)
    got_start = (values[3], values[4])
    got_end = (values[5], values[6]) if len(values) > 6 else None

    want_cutoff = int(round(float(want["tou5_cutoff_soc"])))
    want_amps = round(float(want["tou5_current"]), 1)
    want_start = _hhmm(want["tou5_start"])
    want_end = _hhmm(want["tou5_end"])

    if got_cutoff != want_cutoff:
        problems.append(f"cutoff SOC {got_cutoff} != {want_cutoff}")
    if abs(got_amps - want_amps) > 0.15:
        problems.append(f"strom {got_amps} A != {want_amps} A")
    if want_start and got_start != want_start:
        problems.append(f"start {got_start} != {want_start}")
    if want_end and got_end and got_end != want_end:
        problems.append(f"slut {got_end} != {want_end}")

    # Grinden maste vara satt i periodmasken, annars ar fonstret inte aktivt
    # aven om alla varden star ratt.
    try:
        mask = client.call_service(
            "solis_modbus",
            "solis_read_register",
            {"address": TOU_BITMASK_REG, "register_type": "holding", "count": 1},
            return_response=True,
        )
        mask_values = _register_values(mask)
        if mask_values:
            if not (mask_values[0] & (1 << TOU_SLOT5_BIT)):
                problems.append(
                    f"periodmask {mask_values[0]} saknar bit {TOU_SLOT5_BIT} "
                    "(period 5 ar inte aktiv)"
                )
        else:
            problems.append(f"periodmask olasbar: {str(mask)[:80]}")
    except Exception as exc:
        problems.append(f"periodmask olasbar: {exc}")

    if problems:
        logger("[VERIFIERING] MISSMATCH: " + "; ".join(problems))
        return False

    # Importtaket maste vara AV i laddlage. Ett kvarlamnat tak kapar
    # laddningen till (tak - huslast) utan att nagon skrivning ser fel ut:
    # matt 2026-11-24 22:00-23:00 gav 5000 W tak och 2571 W huslast 1970 W
    # laddning i stallet for planens 4000 W.
    try:
        cap_on = client.call_service(
            "solis_modbus",
            "solis_read_register",
            {"address": GRID_PEAK_SH_CTRL_REG, "register_type": "holding", "count": 1},
            return_response=True,
        )
        cap_values = _register_values(cap_on)
        if cap_values:
            if cap_values[0] & (1 << GRID_PEAK_SH_BIT):
                logger(
                    f"[VERIFIERING] importtaket star PA (43483 = {cap_values[0]}) "
                    "- laddningen kapas till (tak - huslast)"
                )
                return False
        else:
            logger(f"[VERIFIERING] importtaket olasbart: {str(cap_on)[:80]}")
            return False
    except Exception as exc:
        logger(f"[VERIFIERING] importtaket olasbart: {exc}")
        return False

    logger(
        f"[VERIFIERING] slot 5 bekraftad: {got_amps} A, "
        f"{want['tou5_start']}->{want['tou5_end']}, cutoff {got_cutoff} %, "
        "importtaket AV"
    )
    return True


# --- Verkstallande -----------------------------------------------------------


def execute(actions: list[dict], client, dry_run: bool = False, logger=print) -> list[dict]:
    """Utfor aktionerna i ordning. Returnerar de som (skulle ha) skrivits."""
    done = []
    for a in actions:
        ent = a["data"].get("entity_id", "")
        val = a["data"].get("value", a["data"].get("time", ""))
        if dry_run:
            logger(f"[DRY RUN] {a['domain']}.{a['service']} {ent} = {val}")
            done.append(a)
            continue
        try:
            client.call_service(a["domain"], a["service"], a["data"])
            logger(f"[LIVE] {a['domain']}.{a['service']} {ent} = {val}")
            done.append(a)
        except Exception as exc:  # en skrivning far inte stoppa resten
            logger(f"[FEL] {a['domain']}.{a['service']} {ent} = {val}: {exc}")
    return done


def describe(mode: str) -> str:
    """Kort beskrivning av laget - anvands i status-sensorn."""
    return {
        MODE_CHARGE: "Uppladdning (TOU slot 5)",
        MODE_DISCHARGE_BATTERY: "Urladdning, batterieffekt (Self-use)",
        MODE_DISCHARGE_GRID: "Urladdning, nateffekt (Peak shaving)",
        MODE_IDLE: "Inaktivt batteri (Self-use)",
        MODE_IDLE_GRID: "Inaktivt batteri, nateffekt (Peak shaving)",
    }.get(mode, f"Okant lage: {mode}")
