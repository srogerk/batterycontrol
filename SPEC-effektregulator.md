# SPEC: Effektregulatorn — batteristyrning mot effektavgift (14 kW-avtal)

*Status: v1.2, 2026-11-19 (urladdningsaktuatorn `backup_soc` mätt och verifierad,
se §2.4). v1.1, 2026-10-27 (GE-svar av Roger infogade). Simulerad mot 2025 års
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

### 2.4 Urladdningsaktuatorn `backup_soc` — mätt 2026-11-19

**Detta avsnitt ersätter den tidigare beskrivningen av `backup_soc` som enbart en
"spärr".** Mätningen visar att registret är ett **börvärde** som verkar i två riktningar,
och att riktningen avgörs av bit 5 i register 43110.

#### Mätbeviset (kausalt, inte korrelation)

Test 22:20 med `switch.allow_grid_to_charge_the_battery` **avstängd** (bit 5 = 0,
`sw_value` 305 → 273) — då kan ingen nätladdning ske, och golvet observeras ensamt:

| tid | `backup_soc` | bit 5 | SOC | urladdning | nätimport | huslast |
|---|---|---|---|---|---|---|
| 22:19:55 | 15 | på | 17 % | 2 767 W | 0 W | 2 642 W |
| **22:20:09** | **19** | **av** | 17 % | **672 W** | **2 635 W** | 3 232 W |
| 22:20:23 | 15 | av | 17 % | 0 W | 50 W | 2 575 W |
| 22:20:30 | 15 | av | 17 % | **2 742 W** | 0 W | 2 636 W |

Golv **över** SOC ⇒ urladdningen stryps och nätet tar huslasten. Golv **under** SOC
igen ⇒ urladdningen återupptas inom samma minut. `backup_soc` är alltså
**urladdningsgolvet i Self-Use**, och det verkar kontinuerligt (inte binärt).

#### Samma värde är också ett laddningstak

Med bit 5 **på** (normalläget, `allow_grid_to_charge_the_battery` = on) blir samma
börvärde ett **påfyllningsmål**: invertern laddar batteriet **från nätet** upp till
`backup_soc`. Uppmätt samma kväll:

| tid | `backup_soc` | SOC | nätladdning | import |
|---|---|---|---|---|
| 20:39:32 | 95 | 27 % | 8 939 W | 12 753 W |
| 20:44:32 | 30 | 27 % | 7 442 → 9 068 W | 12 779 W |
| 22:15:08 | 21 → 18 | 18 % | 8 263 W | 12 487 W |
| 22:16:04 | 18 (= SOC) | 18 % | 9 235 W | 12 705 W |

**Slutsats:** `backup_soc` är **ett börvärde, inte två funktioner**. Ligger börvärdet
över aktuell SOC fyller invertern på (om bit 5 tillåter nätladdning), och under
börvärdet urladdar den. Det förklarar varför tidigare tester såg ut att motsäga
varandra: **bit 5 är alltid på i normalläget, så laddbeteendet maskerade alltid
golvtestet.**

#### Varför det inte kunde avgöras tidigare

Tidigare hypotesprövning (2026-11-19 20:30–20:36) satte golv 95/100 med urladdning
pågående och såg **ingen** effekt — varken stopp eller laddning. Skillnaden mot de
fungerande fallen är **bit 1 (TOU)**: `sw_value` var då 307 (bit 1 på), medan de
fungerande fallen hade 305. TOU-läget tycks undertrycka reservpåfyllningen. Det är
en hypotes, inte en mätning — se §8.

#### Tre konsekvenser för regulatorn

1. **Golvet är den urladdningsaktuator vi letat efter.** Det finns alltså en
   kontinuerlig urladdningskanal — orden "urladdning är inte styrd, bara tillåten
   eller förbjuden" i `GLOBAL-OPTIMERING-MODELL.md` §3.8 är för starka och ska
   nyanseras: Self-Use parerar huslasten, men **golvet bestämmer hur mycket av
   huslasten batteriet får ta**.
2. **Bit 5 måste hanteras uttryckligen.** Så länge bit 5 är på är varje höjning av
   `backup_soc` över SOC en **nätladdningsorder**. Regulatorn får aldrig sätta ett
   golv över uppmätt SOC utan att veta att den köper energi.
3. ~~**Avrundningen är en risk.**~~ **MOTBEVISAD 2026-11-21 — se §2.4a.**
   `round()` var inte boven. SOC-sensorn (input-register 33139) är ett **heltal**:
   1 326 mätpunkter 14–21 nov innehöll inte ett enda decimalvärde, så `round()`,
   `floor()` och `int()` ger identiskt resultat. Rådet att byta till `floor()`
   hade varit en **no-op**. Felet satt i att `golv = SOC` överhuvudtaget användes
   som "lås" — se §2.4a.

### 2.4a Ett "håll" kan inte uttryckas i `backup_soc` — mätt 2026-11-21

§2.4 visade *att* `backup_soc` är ett börvärde i två riktningar. Mätningen 21 nov
visar **varför det gör "lås vid nuvarande SOC" till en nätladdningsorder**, och
varför felet låg i drift i veckor utan att synas.

#### Mekanismen

SOC-sensorn (input-register 33139) har **1 % upplösning och är ett heltal**
(1 326 mätpunkter 14–21 nov, noll decimalvärden). Det **visade** värdet är en
**övre gräns** för det interna värdet — invertern överskjuter ett helt steg innan
den ser att målet är nått (uppmätt 0,97 % medel över 28 fulla steg).

Därför finns **inget** värde på 43024 som betyder "håll", så länge bit 5 är på:

| golv vs SOC | resultat |
|---|---|
| golv **>** SOC | nätladdning upp till golvet (reservvägen, ej effektreglerad) |
| golv **<** SOC | Self-Use laddar ur ned mot golvet |
| golv **=** SOC | **omöjligt att träffa** — det visade värdet ligger på eller över det verkliga, så det hamnar i praktiken över |

**En håll-funktion måste därför uttryckas i bit 5, inte i golvet.** Det är den
egentliga motsägelsen — inte avrundningen.

#### Vad felet kostade (mätt i drift)

Koden skrev i vänteläge `golv = round(SOC)` för att "låsa" batteriet. Varje kvart
blev därför en laddorder, och reservvägen går på `43012` = 50,0 A (appen skriver
aldrig det registret) — alltså okontrollerad effekt.

| mått | värde |
|---|---|
| Laddstötar 2026-11-21 | **44**, mot 0–7 per dag veckan innan |
| Andel kvartar 03:00–10:00 med stöt | **93 %** (26 av 28); utan stöt endast 03:00 och 03:15 |
| Energi per stöt | 0,19–0,30 kWh = 0,75–1,19 % av batteriet |
| Toppeffekt | 12 282 W import (11:01) |
| Fördröjning från appens skrivning | 4,9–15,6 s, 13 av 13 mätta fall |
| SOC-förlopp | 12 → 50 % utan att planen någon gång begärde nätladdning |

Att det visade SOC-värdet är systematiskt **på eller över** det verkliga bevisas av
att 93 % av kvartarna gav en stöt: vore det `round()` skulle bara ~50 % göra det.

#### Regulatorns kontrakt (gäller från v0.7.2)

- **bit 5** = får batteriet laddas från nätet överhuvudtaget. På **endast** när
  planen faktiskt vill nätladda.
- **43024** = urladdningsgolv. Ett högt golv är ofarligt när bit 5 är av — det kan
  bara strypa urladdning.
- Vid låsning skrivs först `SAFE_FLOOR = 15` (ofarligt även med bit 5 på), därefter
  stängs bit 5, sist sätts mål-golvet. Skälet till den ordningen är att
  skrivlatenserna är **omvända**: 43110 upp till ~40 s, 43024 ~5 s. "Stäng kranen
  först" hade därför inte räckt — golvet hade landat ~35 s *före* kranen och
  lämnat exakt det fönster som gav 8–10 kW. Att aldrig skriva ett farligt värde är
  det som håller, oavsett latens.
- Nedskrivningen till 15 sker bara när bit 5 **kan** vara öppen (bekräftat på, eller
  okänt läge). Är den bekräftat av rörs inget — annars svänger golvet 52 → 15 → 51
  varje kvart och lämnar urladdningsgolvet på 15 i ~5 s (fångat i drift 12:15:01,
  rättat i 0.7.2).

#### Öppen fråga som måste mätas före nästa ingrepp

**Kräver TOU-nattladdningen (slot 1) att bit 5 är på?** Om ja är priset för att
stänga bit 5 för högt och lösningen måste tänkas om. Mätt: slot 1 står på
cut-off SOC 100 %, 05:30–06:00, men **strömmen är 0 A** eftersom planen inte vill
ladda — alltså är vägen ännu inte prövad i skarpt läge. Se §8 p.7.

---

#### Gränsen för vad som är mätt

Allt ovan gäller **Self-Use + Reserve/Backup** (bit 0+4, `sw_value` 305). I **rent
Self-Use** (bit 4 av, `sw_value` 289) gav golv 95 ingen effekt alls vid SOC 27 — utan
Reserve-läget har `backup_soc` ingen funktion. Regulatorn ska därför köra i
Self-Use + Reserve/Backup.

`overdischarge_soc` (register **43011**, spegel 33213) är **inte** ett reglage: satt till 38 hölls det
~75 s och återgick sedan till 14 utan åtgärd. Invertern/BMS äger registret, och det
är detta värde (14 %) som stoppade batteriet 14:50 i stället för vårt golv på 15.

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
| **Urladdningsgolv** | `number.solis_s6_eh1p_backup_soc` (reg 43024) | **Verifierad 2026-11-19, se §2.4.** Börvärde: under värdet urladdar batteriet, över värdet laddar det — från nätet om bit 5 tillåter. 0–100 %, steg 1. |
| **Nätladdningens tillstånd** | `switch.allow_grid_to_charge_the_battery` (bit 5, reg 43110) | **Måste hanteras uttryckligen.** På i normalläget ⇒ varje höjning av `backup_soc` över SOC är en nätladdningsorder. Av ⇒ golvet blir envägs (bara urladdningsstopp). |
| Arbetsläge | `select.work_mode` (reg 43110) | Måste vara **Self-Use + Reserve/Backup** (bit 0+4). I rent Self-Use har `backup_soc` ingen funktion. |
| Laddström batteri | `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_2` | DC-ampere (~0,26 kW/A vid 256 V); 39 A ≈ 10 kW |
| Slot-fönster | `time.solis_s6_eh1p_grid_time_of_use_charge_{start,end}_slot_2` | skrivs med `time.set_value` |
| Slot på/av | `switch.grid_time_of_use_charging_period_2` | |
| Laddstopp SoC | `number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_2` | |
| Peak-shaving | `switch.grid_peak_shaving_power_enable` (bit 7, reg 43483), `number.solis_s6_eh1p_peak_max_usable_grid_power` (reg 43488), `number.solis_s6_eh1p_peak_baseline_soc` (reg 43487) | Lager 0, cap 13 000 W, bas 75 %. **Oprövat som reglerande importtak** — se §8 p. 7. |
| Predbat-läge | `select.predbat_mode` | "Monitor" = pausat, återställ "Control charge & discharge" |
| Mätning | `sensor.grid_import_power`, `sensor.ev_power`, `sensor.battery_power_charge`/`_discharge`, `sensor.battery_soc`, `sensor.household_power_kw` | |
| Batteritak | `sensor.predbat_solis_modbus_0_{charge,discharge}_rate` (8 000 W) | batterisida; TOU-slotar kan gå över |
| RC-registret | `number.solis_s6_eh1p_rc_force_battery_charge_power`, `select.rc_force_charge_discharge` | **fungerar men FÖRBJUDEN** — RC-force slåss mot peak-shaving (21/10: import 18,0 kW, laddningen oscillerade). ~30 s skrivlatens på selecten. Använd TOU-slotar |

**Bitmönster i `sensor.solis_s6_eh1p_storage_control_switch_value` (reg 43110), verifierade
2026-11-19:** `305` = bitar 0,4,5,8 (Self-Use + Reserve/Backup, normalläge) · `307` = 0,1,4,5,8
(samma **med** TOU, bit 1) · `291` = 0,1,5,8 (Reserve av) · `289` = 0,5,8 (rent Self-Use) ·
`273` = 0,4,8 (bit 5 avstängd — används i §2.4-testet). Bit 8
(`battery_forcecharge_peakshaving`) är en **oberoende modifierare** och står på i alla lägen.

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
3. ~~Effektavgift: brutto (import) eller netto efter export?~~ **Besvarat 2026-10-27: brutto.**
4. Exakt startdatum för effektmätningen (1/11?) — **besvarat: 1/11-2026, första debiterade
   timme måndag 2/11 kl 07:00.**
5. ~~Kan peak-shavingen klampa batteriladdning?~~ **Besvarat 26/10: ja, på TOU-vägen.**
6. TOU-laddning flackar (21→22/10) — stabil grund krävs för Lager 1.
7. **Nytt 2026-11-19: kräver TOU-nattladdningen att bit 5 är på?** Om bit 5 kan stängas av
   blir `backup_soc` ett rent envägsreglage utan nätladdningsrisk. Men om TOU-laddningen
   i så fall slutar fungera är priset för högt. **Måste mätas innan något ändras.**
   **Uppdatering 2026-11-21:** frågan är nu **driftkritisk**, se §2.4a. Regulatorn
   håller bit 5 av i vänteläge, så nästa gång planen begär nätladdning prövas vägen
   automatiskt — men det är ingen kontrollerad mätning. Ett avläst läge duger inte:
   slot 1 står på cut-off 100 % och 05:30–06:00 men med **ström 0 A**, så vägen har
   ännu inte gått skarpt. Kontrollerat test: sätt bit 5 av, tvinga slot 1 till känd
   ström i ett fönster, mät om laddning uteblir.
8. **Nytt 2026-11-19: undertrycker bit 1 (TOU) reservpåfyllningen?** Fallen 20:30–20:36
   (`sw_value` 307, bit 1 på) visade varken urladdningsstopp eller laddning, medan
   20:39 och framåt (305, bit 1 av) fungerade. Hypotes, inte mätning.
9. **Nytt 2026-11-19: reglerar bit 7 + `peak_max_usable_grid_power` nätimporten
   kontinuerligt?** Fortfarande oprövat. Kvällens tester var ogiltiga eftersom importen
   redan var 0 — ett importtak kan bara observeras när det finns import att begränsa.
   Giltigt test: ladda batteriet via TOU med ström **över** capet (bevisat väg från
   26–27/10), eller sätt en känd huslast över taket och mät om importen stannar vid capet.

**Formel för nästa kvarts golv (inför implementering):** om bit 5 behålls på måste
regulatorn räkna kostnaden för påfyllningen innan den höjer golvet. Höjning med ΔSOC
kostar `ΔSOC/100 × batterikapacitet` kWh nätenergi till aktuellt spotpris, och den
energin är förbrukad oavsett om den någonsin används för att kapa en topp.
