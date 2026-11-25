# STYRNING VIA ENTITETER — implementerad 2026-11-24

Ersätter den gamla `want_lock` / `want_grid_charge`-logiken i `run_loop.py`.
Källa: Rogers spec 7 a–e (2026-11-23). Se även `ENTITETS-STYRNING.md`.

## Filer

| Fil | Roll |
|---|---|
| `src/battery_control.py` | All styrlogik. Ren funktion `build_actions()`, inga sidoeffekter. |
| `src/run_loop.py` | Anropar modulen; sköter prognos, optimering, elbil, status. |
| `tests/test_battery_control.py` | 72 enhetstester: ett per styrmode + invarianter. |
| `tests/test_run_loop_integration.py` | 19 tester: hela kedjan plan → skrivning. |
| `tests/demo_modes.py` | Visar skrivningarna per mode. |
| `tests/simulate_live.py` | Simulerar mot **levande** plan, skriver inget. |
| `tests/validate_entities.py` | Validerar entitetskartan mot levande HA. |

Kör allt: `python3 -m unittest discover -s tests -v` — **91 tester, alla gröna.**

## De fem styrmoden

| Mode | Trigger | Work mode | Nyckelentiteter |
|---|---|---|---|
| `charge` (a) | `grid_charge_kw > 0` | Self-use | `charge_limit_power` 10 000 W, TOU **slot 5**, allow_grid ON |
| `discharge_battery` (b) | `discharge_kw > 0`, **ej** tariff | Self-use | `discharge_limit_power` = kW×1000 |
| `discharge_grid` (c) | `discharge_kw > 0`, **i** tariff | Peak shaving | `peak_max_grid` = planens nätimport, baseline 100 % |
| `idle` (d) | ingen effekt, **ej** tariff | Self-use | `discharge_limit_power` 0 |
| `idle_grid` (e) | ingen effekt, **i** tariff | Peak shaving | `peak_max_grid` 14 000 W, baseline 100 %, discharge 0 |

**Tariffönstret avgör HUR urladdning/inaktivitet utförs — aldrig OM batteriet laddar.**
Inom tariffönstret styrs på nätimport (det som kostar pengar), utanför på batterieffekt.

## Tre fel som modulen stänger

### 1. `battery_charge_limit_power` = 0 blockerade all nätladdning
Natten 23 nov gav TOU 0 W i timmar medan ström, fönster och grind såg korrekta ut.
Roger satte 10 000 W manuellt 00:58:53 → 2 406 W, sedan 8,3 kW.
Modulen skriver **alltid** 10 000 W i laddläge — och **aldrig** i andra lägen (att
sänka det vore att göra nästa laddkvart omöjlig).

### 2. Bit 8 (Forcecharge Peakshaving) ströp laddningen till noll
43110 = 305 (bitar 0, 4, 5, **8**) → 0 W. 43110 = 49 (0, 4, 5) → 8,3 kW.
Modulen slår **alltid** av `switch.battery_forcecharge_peakshaving` först, i alla lägen.

### 3. Flera aktiva TOU-fönster
Upptäckt 2026-11-24 23:16: slot 1 stod **på** med 02:00–23:00 @ 20 A — nästan hela
dygnet, medan planen sade 4,0 kW. Två samtidiga fönster ger odefinierad styrning.
Modulen **stänger slot 1–4 och 6** i varje läge och använder bara slot 5.

## Ordningsregler (del av kontraktet)

- **Laddläge:** förutsättningarna skrivs **före** grinden slås på. En grind på utan
  förutsättningar ger antingen 0 W (ofarligt) eller okontrollerad effekt.
- **Alla andra lägen:** laddfönstret stängs **först**. Att byta läge medan laddning
  pågår lämnar en kvarts restvärde kvar i invertern.
- **Bit 8 och reservgolvet skrivs alltid först**, i varje läge.
- **En misslyckad skrivning stoppar inte resten** (testat).

## Kvartsfönstret

`start` = kvartens start. `end` = kvartens slut **+1 min**, inte +30 s som spec 7a
säger.

**Varför:** TOU-slotens register `43708..43714` är sju register med
**minutupplösning och ingen sekund** —
`[cut_off_soc, current×0.1, cut_off_V, start_h, start_min, end_h, end_min]`.
Läst av slot 5 (2026-11-24 23:30): `[100, 312, 0, 10, 45, 12, 0]` = 10:45–12:00.
`12:45:30` går alltså inte att uttrycka; det blir 12:45 eller 12:46 beroende på om
integrationen avrundar eller trunkerar, och det är oprövat vilket.

En hel minut ger samma skydd som specens 30 s: är loopen sen behöver den inte klippa
laddningen mitt i en kvart. Överhoppet in i nästa kvart är ofarligt — nästa varv
skriver om fönstret.

**Midnattsklämma:** sista kvarten blir annars `23:45` → `00:01`, dvs. start > slut,
vilket är odefinierat för invertern. `end` kläms då till `23:59`. Den kvarten tappar
sin marginal — bättre än ett fönster enheten inte förstår.

Strömmen räknas om från kW vid uppmätt batterispänning (fallback 360 V utanför
100–500 V) och kläms till BMS:ets 50 A.

## Diffning

`bc.diff_actions()` läser nuvarande tillstånd före varje skrivning och hoppar över
det som redan står rätt. `build_actions()` är oförändrad — den bygger fortfarande
hela läget, vilket är det som gör den testbar och självläkande.

| scenario | byggda | skrivs |
|---|---|---|
| Laddläge, allt redan rätt | 17 | 1 (grinden) |
| Byte inaktivt → laddning | 17 | 10 |
| Urladdning, allt redan rätt | 13 | 0 |

Tre regler:

1. **`tou5_switch` skrivs alltid när den ska på.** Grinden är bekräftelsen att
   förutsättningarna ovan är skrivna. Säger HA redan "on" kan fönstret ändå ha
   stängts av enheten (t.ex. vid omstart), och då lämnas kvarten utan laddning om
   vi hoppar över den. Att slå *av* diffas normalt.
2. **Okänt/`unavailable` räknas som "inte lika"** — skriv hellre en gång för mycket.
3. **En läsning som kastar får aldrig tysta styrningen** — aktionen behålls.

Ordningen är orörd. Diffningen filtrerar bort no-ops, den flyttar inget.

## Verifiering av laddfönstret

Natten 23 nov såg allt rätt ut på entitetsnivå — strömmen stod i slot 5, fönstret var
satt, grinden var på — och effekten var ändå 0 W. **Att skriva rätt är inte samma sak
som att enheten har fått det.**

Efter att grinden slagits på (endast i laddläge, med 2 s fördröjning) läser
`bc.verify_charge_window()` tillbaka:

- `43736..43742` — slot 5:s egna register, jämfört mot det läget *avsåg*
- `43707` — periodmasken, bit 4 måste vara satt (period 5 aktiv)

En kvart är lång tid att vänta på att få veta att laddningen inte startade. Nu syns
det i loggen och som `- VERIFIERING MISSLYCKADES` i statustexten.

Jämförelsen använder **hela** aktionslistan, inte den diffade — den jämför mot vad
läget avsåg, inte mot vad som råkade skrivas.

`_register_values()` hanterar att svaret kommer som dict *eller* JSON-sträng
beroende på transport (REST via curl ger en sträng — verifierat mot levande HA
2026-11-25 00:12).

## Nya attribut på status-sensorn

`sensor.batterycontrol_status` bär nu `control_mode`, `control_mode_text` och
`control_actions` — så att det syns i HA vilken av de fem vägarna som används.
Natten 23 nov kunde felet hålla i sig i timmar just för att ingenting visade det.

## Kvar att besluta (Rogers val)

1. **`discharge_limit_power` = 0 i läge (e).** Specen säger 0, men 0 kan betyda
   "obegränsat" — bevisat: 43131 = 0 **och** 907 W urladdning 00:56:41. Om (e) ska
   hålla batteriet inaktivt kan 0 ge motsatt effekt. Kräver mätning.
2. **`peak_baseline_soc` 100 i (e).** Med baseline ≥ SOC tar invertern allt från
   batteriet vid behov — vilket motverkar "inaktivt". Samma spänning som punkt 1.
3. **`select.work_mode` rörs aldrig.** Modulen använder `switch.self_use_mode` och
   `switch.peak_shaving_mode` i stället, eftersom `select` rensar `STORAGE_MODE_BITS`
   och kan släcka TOU-bit 1 permanent. Switcharnas exakta registerpåverkan är dock
   inte isolerat verifierad.
4. **Driftsättning kräver** commit + push + `BUILD_VERSION` + verifiering av
   `kallkod <hash>` i add-on-loggen. Inte gjort — väntar på Rogers ok.

## 43483 bit 7 — utredd 2026-11-25 (var punkten "rollen är okänd")

`switch.grid_peak_shaving_power_enable` är **inte** en dublett av
`switch.peak_shaving_mode`. Källkoden (`solis_modbus/sensor_data/switch_sensors.py`,
rad 107–122) namnger registret:

```
{"register": 43483, "entities": [
    {"bit_position": 7, "name": "Grid Peak-shaving power enable"}]}
```

43483 = "Hybrid Function Control", ett **flaggregister**. Värdet 1946 =
`0b11110011010` har bitar `{1, 3, 4, 7, 8, 9, 10}` satta — bit 7 är en av sju
oberoende flaggor, inte en lägesbrytare.

### Rollen, bevisad ur Rogers egna takändringar

Ingen mätning behövdes: 43488 ändrades sju gånger det senaste dygnet, vilket ger
ett naturligt A/B-experiment mot nätimporten.

| period | tak (43488) | bit 7 | nätimport max | andel mätpunkter över tak |
|---|---|---|---|---|
| 23 nov 21:42→22:08 | 6 000 W | **av** | 8 041 W | **69,8 %** |
| 23 nov 22:19→23:14 | 2 000 W | **av** (bit 11 **på**) | 7 552 W | 5,0 % |
| 23 nov 23:29→23:58 | 14 000 W | på | 13 211 W | 0 % |
| 24 nov 07:35→21:57 | **1 800 W** | på | 3 734 W | **0,7 %** |
| 24 nov 21:57→00:15 | 5 000 W | på | 4 727 W | 0 % |

**Slutsatser:**

1. **Bit 7 är spärren som får 43488 att gälla.** Med den av är taket verkningslöst
   — 69,8 % av mätpunkterna låg över ett 6 kW-tak.
2. **Bit 11 räcker inte.** 23 nov 22:19–23:14 stod `peak_shaving_mode` **på** med
   tak 2 000 W; importen gick ändå till 7 552 W.
3. Under 24 nov syns mekanismen i klartext: huslast 2 200–3 700 W medan
   nätimporten hölls på **1 628–1 636 W** timme efter timme.

### Följden för koden — en bugg av samma slag som 43130

`build_actions()` skrev bit 11 och 43488 i läge (c)/(e), men **rörde aldrig bit 7**.
Det gav två tysta fel:

- **Läge (c)/(e) var verkningslösa** när bit 7 var av — koden skrev ett tak som
  inte gällde. Ingen skrivning såg fel ut.
- **Laddläget kapades.** Bit 7 stod på med tak 5 000 W; laddläget slog av bit 11 men
  lämnade bit 7. Mätt 24 nov 22:00–23:00: plan 4 000 W, huslast 2 571 W,
  **faktisk laddning 1 970 W**. Taket skiljer inte på huslast och laddning — det
  stryper summan.

### Åtgärd

Bit 7 skrivs nu i **alla fem lägen** och ingår i invariant-testerna:

| läge | bit 11 (`peak_shaving`) | bit 7 (`grid_peak_shaving`) | 43488 |
|---|---|---|---|
| a charge | av | **av** | skrivs inte |
| b discharge_battery | av | av | skrivs inte |
| c discharge_grid | på | **på** | planens `grid_import_kw` |
| d idle | av | av | skrivs inte |
| e idle_grid | på | **på** | 14 000 W |

`verify_charge_window()` läser dessutom 43483 efter grinden och underkänner
laddningen om bit 7 står på — det är den enda läsning som avslöjar kapningen.

**Tester:** 124 (var 120). Två nya invariant-test plus två
verifieringstest. Muterad kod (bit 7 borttagen ur laddläget respektive ur läge c)
fäller dem — bevisat, inte antaget.

**Kvar att mäta:** om bit 7 = på också påverkar *urladdning* mot nätet i läge (b).
Taket arbetar på samma summavariabel, så det bör göra det, men det är inte mätt.

