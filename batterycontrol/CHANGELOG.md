# Changelog

## 0.8.0 — 2026-11-25

### Nytt: styrningen flyttad till en egen modul, läge per kvart

All styrning sker nu via `src/battery_control.py` — en ren modul som bygger en
lista åtgärder och skriver **enbart HA-entiteter**, aldrig Modbus-register.
Fem lägen: `charge`, `discharge_battery`, `discharge_grid`, `idle`, `idle_grid`.
Laddning går före allt annat; tariffönstret avgör **hur** urladdningen sker,
aldrig **om** laddning ska ske.

Diffning: en åtgärd skrivs bara när värdet faktiskt avviker från det lästa.
Mätt i laddläge: 17 åtgärder → 1 skrivning när inget behöver ändras.

Verifiering: efter att laddfönstret öppnats läses slot 5:s register tillbaka
(`43736..43742` + periodmask `43707`) och jämförs med det som skrevs.

### Rättat: importtaket 43488 var verkningslöst utan bit 7

`peak_max_usable_grid_power` (43488) håller bara importen under taket om
`switch.grid_peak_shaving_power_enable` (43483 bit 7) är **på**. Utan den gick
importen till 8 041 W mot ett tak på 6 000 W — 69,8 % av minuterna över tak.
Med bit 7 på: 0,7 % över, och nattimporten låst på 1 628–1 636 W timme efter
timme. Bit 11 (`peak_shaving_mode`) räcker alltså inte.

Koden satte bit 11 och 43488 men rörde aldrig bit 7, vilket gjorde läge
`discharge_grid` och `idle_grid` verkningslösa. Värre: med bit 7 på ströps
**laddningen** — 24 nov 21:57–23:00 bad planen 4,0 kW, huslasten var 2 571 W,
taket 5 000 W, och faktisk laddning blev 1 970 W.

Bit 7 sätts nu **av** i `charge`, `discharge_battery` och `idle`, och **på** i
`discharge_grid` och `idle_grid`.

### Rättat: laddfönstrets sluttid låg 1 minut för tidigt

Slot-registren har minutupplösning utan sekund. Slutet sätts nu till kvartens
slut + 1 minut (`END_MARGIN_MINUTES = 1`), med klämning till 23:59 vid midnatt.

### Rättat: urladdningsgränsen 0 gav FRI urladdning

Spec 7d/7e angav `battery_discharge_limit_power = 0` för "batteriet inaktivt".
Skåpmatning 2026-11-25 visar att 0 är **ogiltigt** i enheten och ger motsatsen
— batteriet laddade ur fullt.

| entitet | register | faktisk urladdning | utfall |
|---|---|---|---|
| 10 000 W | 1 000 | 3 069 W | fri (referens) |
| 1 000 W | 100 | 1 045 W | taket biter |
| 500 W | 50 | 550 W | taket biter |
| 200 W | 20 | 226 W | taket biter — lägsta fungerande |
| 100 W | 10 | 3 004 W | biter INTE |
| 50 W | 5 | — | avvisas tyst |
| 0 W | 0 | 3 064 W | fri |

Två fallgropar: **entiteten räknar watt, registret tiotal watt** (faktor 10), så
entiteten 50 W blir register 5 och ligger under enhetens minimi — då avvisar
enheten tyst och behåller sitt gamla värde (syns i HA:s loggbok som en ändring
med `user=None`). Och 100 W ligger precis på specens angivna minimigräns men
biter ändå inte. Entitetens `min=0, max=655350` är registergränser, inte
enhetens verkliga.

Läge `idle` och `idle_grid` skriver nu `DISCHARGE_LIMIT_IDLE_W = 200`.

### Rättat: verifieringen läste tillbaka för snabbt

En läsning 12 s efter skrivning visar cachens eftersläpning och ger falska
avvisanden. Acceptans verifieras nu med faktisk effekt över minst 90 s, eller
via loggbokens `context_user_id` (`None` = enhetens eget värde).

## 0.7.2 — 2026-11-21

### Rättat: onödig golvsvängning varje kvart

0.7.1 skrev det säkra golvet (15) **varje** kvart i vänteläge, även när bit 5
redan var bekräftat av. Det gav två extra skrivningar per kvart och — värre —
under ~5 s stod urladdningsgolvet på 15, dvs batteriet kunde laddas ur fritt.
Upptäcktes av bevakningen 12:15:01: `golv 52 -> 15 -> 51`.

Nedskrivningen till 15 sker nu **endast när bit 5 kan vara öppen**: bekräftat på,
eller okänt läge (entiteten `unavailable` efter reload). Är bit 5 bekräftat av är
ett högt golv ofarligt — det kan bara strypa urladdning — och körningen rör inget.

## 0.7.1 — 2026-11-21

### Rättat: självförstärkande nätladdning i vänteläge (rotorsak)

`backup_soc` (register 43024) är inget spärrvärde utan ett **börvärde** som
verkar i båda riktningarna. Med bit 5 (`allow_grid_to_charge_the_battery`) på
betyder **varje** golv över aktuell SOC en nätladdningsorder.

Koden skrev i vänteläge `golv = round(SOC)` för att "låsa" batteriet. Eftersom
SOC-sensorn har 1 %-upplösning och det visade värdet är en **övre gräns** för
det verkliga hamnade golvet på eller över det verkliga värdet — alltså en
laddorder varje kvart. Reservvägen är inte effektreglerad (går på
`43012` = 50,0 A) och drog 3–12,8 kW tills SOC tickat ett helt steg.

Uppmätt 2026-11-21: **44 laddstötar** mot 0–7 per dag veckan innan. 93 % av
kvartarna 03:00–10:00 gav en stöt, varje stöt 0,19–0,30 kWh = 0,75–1,19 % av
batteriet, alla 5–16 s efter appens skrivning. Batteriet hissades 12 → 50 %
utan att planen någon gång begärde nätladdning.

**Ändringar i `src/run_loop.py`:**

- Rolluppdelning: **bit 5** = får batteriet laddas från nätet överhuvudtaget,
  **43024** = urladdningsgolv. Ett "håll" uttrycks numera i bit 5, aldrig i golvet.
- Bit 5 är på **endast** när planen faktiskt vill nätladda.
- Sekvensen är säker mot de omvända skrivlatenserna (43110 upp till ~40 s,
  43024 ~5 s): vid låsning skrivs först `SAFE_FLOOR = 15` (ofarligt även med
  bit 5 på), därefter stängs bit 5, sist sätts mål-golvet. Inget ögonblick har
  högt golv och öppen kran samtidigt — oavsett latens.
- `grid_charge_allowed` publiceras som attribut på `sensor.batterycontrol_plan_now`
  så att felet inte kan hålla i sig osynligt igen.

**Not:** `round()` var inte boven i sig — SOC är heltal (1326 mätpunkter 14–21 nov,
noll decimalvärden), så `floor()` hade varit en no-op. Felet satt i att `golv = SOC`
överhuvudtaget användes som "lås".
