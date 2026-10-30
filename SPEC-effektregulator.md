# SPEC: Effektregulatorn — batteristyrning mot effektavgift (14 kW-avtal)

*Status: v1.1, 2026-10-27 (GE-svar av Roger infogade). Simulerad mot 2025 års
timdata (101 vinter-vardagar). Projekt: effekttariff-batteristyrning.
Skriven för Roger + implementering i HA.*

---

## 1. Avtal & regler (Göteborg Energi, avtal 111877500009, från 2026-01-01)

| Post | Värde | Konsekvens |
|---|---|---|
| Effektavgift högpris | **135,00 kr/kW·mån** | snittet av månadens **3 högsta timmar, fördelat på 3 olika dygn** |
| Högpristimmar | helgfria vardagar 07–20, **1 nov – 31 mars** | bara de timmarna räknas |
| Lågpris | 0,00 kr/kW | kväll, natt, helg, apr–okt = avgiftsfritt |
| Effektnivå | **14 kW timmedeleffekt, hela året** | >3 överträffar på 12 mån → tvångsbyte till 43 kW-modell |
| Fast avgift | 4 440 kr/år | styrs av effektnivån (43 kW = dyrare) |
| Elöverföring + energiskatt | 6,5 + 45 öre/kWh, **platt över dygnet** | bara effektavgiften varierar med tid |

**Två tal att styra mot:**
- **Topp-3 (nov–mar):** varje kW i topp-timmarna ≈ 45 kr/mån (135/3). Säsongsreplay
  2025: 4 353 kr oförändrat → 3 082 (EV flyttat) → ~1 000–1 200 (batterijämnning).
- **14 kW (hela året):** 2025 gav 9 timmar >14 kW på 5 dygn (alla EV/nödladdning) —
  avtalsbrott i praktiskt taget en kväll. **Denna gräns är prioritet 1.**

**Bekräftat av GE (Roger, 2026-10-27):**
- **Toppar räknas som max-top per dygn** — effektavgiften = medlet av de 3 högsta
  dygnestopparna på 3 olika dygn (Roger: "min 3 månadstoppar ligger alla på olika
  dygn"). 3-ggrs-regeln för 14 kW läses på samma sätt: en övertrassering = ett dygn
  vars max-top > 14 kW (2025 = 5 dygn, inte 9 timmar — över 3 båda vägarna, så
  slutsatsen "2025 års beteende = tvångsbyte i november" gäller oförändrat).
- **Effektavgiften räknas på BRUTTOIMPORT** — export (sol/batteri) kvittas inte.
  Omspelningarna (4 353 / 3 082 / ~1 200 kr) är gjorda på timvis import = brutto →
  giltiga som de är. Ingen exportstrategi kan sänka topparna; bara lägre import.
- **Mätningen startar 1/11-2026.** 1/11 är en söndag (helg = lågpris) → **första
  debiterade timmen är måndag 2/11 kl 07:00.**

Kvar okänt: gäller 24/31 dec som helgfria vardagar (båda är torsdagar)? Tills vidare
behandlas de som avgiftsskyddade vardagar (regulatorn kör) — säkert val: onödig
jämnning kostar några kronor, en obevakad toppdag kan kosta hela toppen.

---

## 2. Arkitektur — tre lager, en ägare per fönster

### Lager 0 — Hård gräns 13 kW (hela året, hela dygnet)
- Skydd idag: Solis **Grid Peak-Shaving**, cap 13 000 W (satt 2026-10-21:
  `switch.battery_forcecharge_peakshaving` + `number.solis_s6_eh1p_peak_max_usable_grid_power`).
- ✅ **Klamp under TOU-laddning verifierat 26/10 natt (B1/B2/B3):** kommando
  39/48/50 A (≈10,7/13,2/13,8 kW) levererade 9,4/11,1/12,0 kW — klampen trimmar
  laddströmmen så att importen hålls **platt på 12,79–12,85 kW** (max 12,88),
  oavsett huslast. Klampförmågan är mjuk (ingen urladdning behövs) på TOU-vägen.
- **Effektivt tak ≈ 12,85 kW** vid inställning 13 000 W (CT-kalibreringsmarginal).
  Mjukvarulagret ska sikta ≤ 12,5 kW så att klampen sällan blir aktör — den är
  skyddsytan, inte regulatorn.
- Regel som måste gälla oavsett: **summan EV + batteriladdning + huslast ≤ 13 kW.**
  Vid nattladdning: `batteriladdning = clamp(13 − EV − hus − 2 (marginal), 0, max)`.
  **RC-force (`rc_force_*`) får aldrig användas** — se 21/10-analysen nedan.
- Larm: löpande timme-medel > 12 kW → röst/larm; > 13 → nödstopp av laddning.

**21/10-analysen (varför klampen då INTE höll — löst 2026-10-27 via HA-historik):**
21/10 kl 22:22–22:32 klampade peak-shavingen inte (import medel **14,4**, max
**18,0** med RC 10 kW + EV 5,4 kW), trots att switchen (22:09) och cap 13 000
(22:12) var satta. RC var **inte** ignorerat — registret tog 10 000 W 22:22:07 och
selecten "Solis RC Force Battery Charge" satte sig 22:22:38 (~30 s skrivlatens;
testloggens "None"-avläsning var förtidig). Inga TOU-slotar var öppna under testet.
**Mekanism (trolig):** RC-force = hård effektsetpunkt som klampens trimning slåss
mot → batteriladdningen oscillerade 4↔10 kW, importen 10↔18. TOU-ström = tak som
klampen mjukt trimmar (B-stegen 26/10: platt 12,8). Kvarstår oprövat: EV +
TOU-laddning under klamp — mjukvaruregeln EV+ladd+hus ≤ 13 kW håller trimdjupet litet.

### Lager 1 — Nattladdning, klar 06:55
- Laddmål = N̂(dagens 07–20-behov, EV-fritt) + 5 kWh marginal, dock ≤ batteriets
  kapacitet (~23 kWh användbart av 25,6).
- Placera laddningen på spot-låga timmar (nätavgiften är platt — bara spot styr).
  Hänsyn till Lager 0 om EV laddar parallellt.
- Predbat kan få äga denna fas (dämpad), alternativt egen TOU-styrning — men bara
  en skrivare åt gången (se §5).

### Lager 2 — Effektregulator 07–20, vardagar 1 nov–31 mars
Se §3. Utanför fönstret: **fritt läge** — spotarbitrage, EV-laddning, bastu.
Endast Lager 0 gäller där.

---

## 3. x-algoritmen (self-use offset)

**Idé (Roger):** self-use ska inte kicka in vid 0 kW behov utan vid **x** — behovet
under x köps på nätet (billiga timmar), batteriet täcker det över x. Så blir
x-budgeten (≈ x·13 kWh) fönstrets inköpsbudget på spot, batteriet bär topparna.

### 3.1 Exakt form (använd denna)
> **Lös x ur: Σ_h max(0, behov_h − x) = B_eff**

- `behov_h` = timprognos för husbehov 07–20 (13 timmar), EV-fritt
- `B_eff = B₀ − E_kväll`, B₀ = batteriets kWh kl 07:00, E_kväll = sparat till
  dyra kvällstimmar efter 20:00 (föreslaget 3–5 kWh; spot-priset efter fönstret
  är inte effektavgiftsskyddat)
- Geometriskt: x = "vattenfyllningsnivån" — den **lägsta möjliga dags-topp** som
  batteriet räcker till. Ekvationen är alltså både formel och optimering.
- Specialfall: Σ behov ≤ B_eff → x = 0 (batteriet täcker hela dagen — kör på).

*Förenklad form (Roger):* `x·13 + B = dagens troliga maxbehov` — fungerar när
behovet > x varje timme. Den exakta formen vinner när dagarna har dipp-mitt-på-dagen
(då slösar x·13-budgeten timmar under taket som batteriet ändå inte behöver).

### 3.2 N̂ — dagsprognos för behovet (EV-fritt)
1. **Init per månad** (p90 av 2025): nov 30 / dec 38 / jan 34 / feb 27 / mar 20 kWh.
   (p90, inte medel — medel lämnar batteriet tomt hälften av dagarna.)
2. **Justera:** temp (−1,0…−1,7 kWh/°C mot dygnsmedel — brett intervall, modellen
   är svag), vind (komplement, kylning), **solprognos (avdrag feb–mar)**, EWMA på
   senaste 5 vardagarna, flagga för planerad stor last (bastu ≈ +8–10 kWh, tork ≈ +3).
3. **Kalibrering:** jämför prognos med mätning kl 20:00 varje dag. Batteritomt
   före 20:00 flera dagar i rad → höj init (övergå till nästa p-nivå).

### 3.3 Regler per timme (07:00–20:00, vardag, nov–mar)
```
x = lös(Σ max(0, behov_h − x) = B_eff)      # kl 06:55, låses för dagen
varje timme h:
    import_h = behov_h − urladdning_h
    mål: import_h ≤ x                         # hard ceiling = dagens dags-topp
    behov_h > x  → batteriet urladdar (behov_h − x)
    behov_h ≤ x  → köp upp till x på fönstrets billigaste spot-timmar
                   (batteriet sparas till dyra timmar + kvällen)
om batteriet riskerar att ta slut före 20:00:   # even-spread-fallback
    x' = max(x_min, kvarvarande_behov / kvarvarande_timmar)
    (jämn dag → låg dags-topp; det är toppen som kostar, inte energin)
```
**x-intervall:** x_min ≈ 1,0 kW, x_max ≈ 2,5 kW. x = 0 bara när prognosen säger
att batteriet täcker hela dagen (sim: fast x=0 var sämst — batteritomt 78/101
dagar, +500 kr/säsong; x=1,0–1,5 var bäst; golvet hjälper mest när batteriet är
litet/delsålt). "Ambitiöst" (lågt x) tömmer batteriet exakt kl 20:00 — då är det
inget kvar till kvällens dyra spot. E_kväll (§3.1) är motvikten.

### 3.4 Simuleringsresultat (2025, 101 vinter-vardagar, EV flyttat ur fönstret)
Säsongsavgift nov–mars, batteri kl 07:00 = B:

| x-policy | B=15 kWh | B=23 kWh | Timmar över taket efter tomt batteri (B=15) |
|---|---|---|---|
| x = 0 fast (self-use från noll) | 1 871 kr | 1 459 kr | 501 ⚠️ |
| fast x = 1,0 | 1 504 | 1 042 | 111 |
| fast x = 1,5 | 1 413 | 1 150 | 41 |
| fast x = 2,0 | 1 482 | 1 433 | 10 |
| adaptivt x (p90-formel) | 1 358 | **1 024** | 55 |
| adaptivt, perfekt dagsprognos | 1 129 | 592–786 | 21 |

Referenser: oförändrat 4 353 kr, endast EV-flytt 3 082 kr.

---

## 4. EV-laddning
- **EV utanför fönstret** (nätter/helger) — i praktiken redan sant (2025: 227 av
  2 564 kWh inom fönstret). Största enskilda vinsten: 3 082 kr kvar efter flytten.
- **Nödladdning är huvudfienden** (10–15,2 kW, nov–dec 2025 = alla 14 kW-brott
  + decembers topp-3). Regel: max effekt sätts av Lager 0-summan.
- Tillåtet undantag: långsam laddning inom fönstret (≤ x, ~1,5 kW) — hjälper
  effektavgiften noll och är spot-optimalt om elen är billig.
- EV-styrning idag: Charge Amps Halo (app), ingen SoC-data. Styr ev. via
  tidschema + effekttak i appen.

---

## 5. En ägare per fönster (dual-writer-fällan)
- **07–20 nov–mar:** egen effektregulator äger Solis-aktuatorerna.
- **Övrig tid:** Predbat (dämpad) äger — spotarbitrage som vanligt.
- Växling via state-machine (t.ex. `input_select.regulator_phase`); aldrig två
  skrivare parallellt — Predbat skriver om TOU-slotar i förväg (känd fälla).

---

## 6. Aktuatorer & sensorer (HA, ha.hv39.se)

| Växel | Entity | Not |
|---|---|---|
| Laddström batteri | `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_2` | DC-ampere (~0,26 kW/A vid 256 V); 39 A ≈ 10 kW |
| Slot-fönster | `time.solis_s6_eh1p_grid_time_of_use_charge_{start,end}_slot_2` | skrivs med `time.set_value` |
| Slot på/av | `switch.grid_time_of_use_charging_period_2` | |
| Laddstopp SoC | `number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_2` | |
| Peak-shaving | `switch.battery_forcecharge_peakshaving`, `number.solis_s6_eh1p_peak_max_usable_grid_power` | Lager 0, cap 13 000 W |
| Predbat-läge | `select.predbat_mode` | "Monitor" = pausat, återställ "Control charge & discharge" |
| Mätning | `sensor.grid_import_power`, `sensor.ev_power`, `sensor.battery_power_charge`/`_discharge`, `sensor.battery_soc`, `sensor.household_power_kw` | |
| Batteritak | `sensor.predbat_solis_modbus_0_{charge,discharge}_rate` (8 000 W) | batterisida; TOU-slotar kan gå över |
| RC-registret | `number.solis_s6_eh1p_rc_force_battery_charge_power`, `select.rc_force_charge_discharge` | **fungerar men FÖRBJUDEN** — RC-force slåss mot peak-shaving (21/10: import 18,0 kW, laddningen oscillerade). ~30 s skrivlatens på selecten. Använd TOU-slotar |

EV: Charge Amps (hass-chargeamps) — styrkälla app/HA? (öppen fråga).
API: `https://ha.hv39.se/api`, token i `/workspace/tmp/ha_token`.

---

## 7. Verifieringsplan
1. ✅ Sim mot 2025 års timdata (denna spec, §3.4).
2. ⏳ **Klartest 26→27/10:** (a) når TOU-laddningen batteriet? (21/22 okt: kommanderad,
   0 W — måste vara stabil för Lager 1), (b) klampar peak-shaving import under
   laddning? Fas A: EV + batteriladdning (kväll). Fas B: batteri ensamt 48 A (01:30).
   Avbryt: import > 18 kW, timprojektion > 13, batteri > 14,5 kW.
3. När Lager 2 byggs: körs mot HA i skugg-läge (logga x-beslut + jämför med mätning)
   minst 2 veckor innan aktivering.

## 8. Öppna frågor
1. 3-ggrs-regeln: per timme eller per dygn?
2. 24/31 dec — räknas som vardagar?
3. Effektavgift: brutto (import) eller netto efter export? (sannolikt brutto)
4. Exakt startdatum för effektmätningen (1/11?)
5. Kan peak-shavingen klampa batteriladdning? (testas 26/10)
6. TOU-laddning flackar (21→22/10) — stabil grund krävs för Lager 1.
