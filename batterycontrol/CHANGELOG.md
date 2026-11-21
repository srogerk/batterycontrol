# Changelog

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
