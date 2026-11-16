# Granskning av README (origin/main `5aee96a`) mot koden

*Underlag: `git show origin/main:README.md` (176 rader), jämförd mot `src/` i arbetsträdet
2026-11-13. Alla kodpåståenden nedan är verifierade med grep/läsning eller live-anrop mot HA.*

---

## 1. Kapitel 1 — kan det göras tydligare?

### 1.1 Strukturprincipen håller inte helt: det finns tre kategorier, inte två

Principen "huvudfunktioner = direkta kundnyttor, övriga stöder dem" är rätt tänkt, men
§1.2 innehåller i dag **tre olika sorters saker** som inte är samma sak:

| Sort | Exempel i §1.2 | Problemet |
|---|---|---|
| **Kundnytta** | Fri Self-Use dagtid (parerar transienter) | Detta *är* en nytta — elkunden slipper nättoppar från kaffebryggaren. Hör i §1.1. |
| **Mekanism** | Dynamiskt effekttak (P_dag), Säsongsgolv, Urladdningsspärr | Beskriver *hur* en nytta uppnås, inte en egen nytta. |
| **Implementationsdetalj** | Solis-anpassad laddreglering, Strikt lokal tidszon | Hör i kapitel 2–3, inte i funktionslistan. |

**Förslag:** tre underrubriker i stället för två — `1.1 Kundnyttor`, `1.2 Hur nyttorna uppnås`
(mekanismer), och flytta rena implementationsdetaljer till kapitel 2. Det gör att varje rad i
§1.1 kan läsas som ett svar på "vad får jag?".

### 1.2 `Dynamiskt effekttak (P_dag)` dubblerar `Effektavgiftsminimering`

§1.1 *Effektavgiftsminimering* och §1.2 *Dynamiskt effekttak (P_dag)* beskriver **samma sak
ur två vinklar** — den första som nytta, den andra som mekanism. Läsaren får intrycket av två
funktioner. Slå ihop dem: nyttan i §1.1, och en rad i mekanismavsnittet om *hur* (LP-variabeln).

Notera också: **`P_dag` som namngiven LP-variabel finns bara i `optimizer.py` (v1)**. I
produktionskoden `optimizer_v2.py` heter storheten `p_thresh` per tariffdag och är
`max(P_tak, P_3, P_max)` — ingen `P_dag`-variabel med säsongsgolv som undre gräns. Om kapitel 1
ska beskriva det som faktiskt körs bör termen bytas.

### 1.3 `Fri spotoptimering under taket` är ingen funktion

"Verkligt spotpris utan artificiella påslag" är en **egenskap hos målfunktionen**, inte något
systemet gör. Den beskriver dessutom en frånvaro (inget påslag) — svår att förstå som nytta.
Antingen stryks raden, eller formuleras om till vad den ger kunden: *att batteriet laddas i de
faktiskt billigaste kvartarna, inte i de minst dyra*.

### 1.4 Konkreta textfel

| Rad | Nuvarande | Bör vara |
|---|---|---|
| 12 | "tas hänsyn tas till batteriets slitagekostnad" | "tas hänsyn till batteriets slitagekostnad" |
| 13 | "Effekttariffönstert är 7 - 20 helgfria vardagar" | "Effekttariffönstret är 07:00–20:00 helgfria vardagar" |
| 13 | "Prismodellen är utformad som följer: Priset beräknas på..." | "Prismodellen: priset beräknas på..." |
| 14 | "hålla nere på-av frekvensen" | "hålla nere antalet på/av-cykler" |
| 25 | "smetas därmed ut helt platt över alla 15-mintersperioder" | "15-minutersperioder" |

### 1.5 Kap 1 påstår saker som inte är implementerade

Detta är den viktigaste invändningen — se avsnitt 3 nedan för detaljer:

- **Rad 21: "2-timmarsblock (8 kvartar à 5,5 kW = 11 kWh)"** — koden bokar **3 timmar**
  (`ev_block.py`: `BLOCK_HOURS = 3.0`, `BLOCK_KW = 5.5`) = **16,5 kWh**. Samma dokument säger
  3 timmar i §5 (rad 136) och 2 timmar i §5 (rad 140). Tre olika tal i samma fil.
- **Rad 22: "hembatteriet säkerställs full laddning (100 %) inför effekttariffstart kl 07:00"**
  — det finns **inget sådant tvång** i `optimizer_v2.py` (0 träffar på `hour == 7`). Det är en
  beskrivning av ett typiskt utfall, inte en regel. Formulera som förväntat beteende.
- **Rad 31–38: laddtrappan och dämptermerna är v1** — se avsnitt 3.1.

---

## 1b. 🚨 Allvarligare än något README-fel: `origin/main` kan inte köra

Detta hittades under granskningen och har prioritet över allt annat i dokumentet.

**Fyra moduler på `origin/main` importerar `paths`, men `src/paths.py` finns inte där:**

```
origin/main:src/forecast.py     -> from . import paths
origin/main:src/ha_client.py    -> from . import paths
origin/main:src/ev_block.py     -> from . import paths
origin/main:src/daily_profit.py -> from . import paths
origin/main:src/paths.py        -> FINNS INTE
```

`src/peak_tracker.py` saknas också på `origin/main`, liksom `batterycontrol/` och
`repository.yaml`. Dessutom har `origin/main` fortfarande `hour_len_h`-buggen:

```
origin/main:src/optimizer_v2.py:314   hour_len_h = len(quarters) * 0.25
origin/main:src/optimizer_v2.py:340   b_ub.append(p_thresh * hour_len_h - ev_energy_hour)
```

**Konsekvens:** HA-add-onen bygger sin image från `main` (`ARK REPO_REF=main` i
`batterycontrol/Dockerfile`). En build från `origin/main` i dag ger en image där prognossteget
kraschar på `ModuleNotFoundError: paths` — och med den buggiga `hour_len_h` kvar. Det som
faktiskt körs och det som är committat har glidit isär.

**Det här är inte ett README-problem.** Det bör åtgärdas före eventuella README-ändringar:
antingen committas arbets­trädet (inkl. `paths.py`, `peak_tracker.py`, `batterycontrol/`,
`repository.yaml`), eller så pinnas `REPO_REF` till en commit som fungerar.

---

## 2. Kapitel 2 och 3 — är de korrekta?

### 2.1 Kapitel 2: arkitekturen beskriver v1

| Påstående | Verkligheten |
|---|---|
| Diagrammet: `optimizer.py` = "Global dynamisk LP (SciPy HiGHS)" | Produktionsvägen är **`optimizer_v2.py`**. `optimizer.py` används bara av `run_loop_v1.py` och av hjälpmodulerna för HA-klienten. |
| Rad 78: "**13 variabler per kvart** (inklusive 5 progressiva laddeffektsteg **och rampningsvariabler**)" | v2 har **11**: 1 grid_load + 5 tiers + 4 (batt_load, pv_load, pv_batt, pv_grid) + 1 dP. Det finns **en** dämpvariabel, inte ett R⁺/R⁻-par. 13 stämmer på v1 (`1 + 5 + 7`). |
| Rad 78: "Lösningen tar ca 0,05 s" | v2 löser typiskt på **0,02–0,1 s**. Marginellt, men v2-docstringen säger "<0,1 s". |
| Rad 80: "`generate_plot.py` ... planeringshorisonten (**11–36 timmar**)" | Horisonten är `range(192)` = upp till 48 h, klippt av tillgänglig Nord Pool-data. 11–36 var v1:s fönster. |
| Rad 79: "`run_loop.py` ... Anropas varje kvart via systemets scheduler (cron)" | Korrekt, men den anropas via **`run_loop_watchdog.sh`** som fångar exitkod ≠ 0. Värt en halv rad — det är den som gör att tysta fel syns. |

**Saknas helt i diagrammet** (alla är delar av den körande kedjan):

- `peak_tracker.py` — läser/skriver P1/P2/P3 och beräknar P_max. Körs **före** optimeraren.
- `rescue_hour.py` — mäter upplupen timenergi och matar `E_nu` till LP:n.
- `ev_block.py` — tidsförankrad EV-bokning.
- `daily_profit.py` — dygnsvinstberäkning till dashboarden.
- `generate_web.py` + `batterycontrol/rootfs/app/ingress_server.py` — dashboarden.
- `ha_client.py` — fabriken som väljer Supervisor-API (add-on) eller SSH (utveckling).
- `paths.py` — sökvägsupplösning som gör samma kod körbar i båda miljöerna.

### 2.2 Kapitel 3: nästan allt stämmer, men tre saker är fel

| Påstående | Verkligheten |
|---|---|
| **Rad 93: "Hårt nätimporttak styrs via Solis inbyggda Grid Peak-Shaving (`number.solis_s6_eh1p_peak_max_usable_grid_power`, register 43488)"** | **Entiteten finns och står på 13 000 W, men koden rör den aldrig** (0 grep-träffar i `src/`). Det hårda taket är LP-raden `MAX_GRID_HOUR_KW = 13.5`. Peak-shaving är konfigurerad i växelriktaren, inte styrd av systemet. |
| **Rad 100: huslast `sensor.solis_s6_eh1p_house_load`** | **Död entitet.** Den finns i HA men har `state: None` — ingen värde. Koden läser `sensor.solis_s6_eh1p_household_load_power` (2 811 W vid kontroll), som är den som fungerar. |
| **Rad 96: "`requests`/`urllib`"** | Endast **`urllib`** används (`ha_client.py`). Inget `requests` i `src/`. |

**Entitetslistan (rad 97–104) är ofullständig.** Koden rör 26 entiteter; README namnger 18
(unika, inklusive den döda ovan). **14 saknas:**
De som saknas och som en läsare behöver för att sätta upp systemet:

```
input_number.batterycontrol_p1 / _p2 / _p3        ← månadens toppar (peak_tracker)
number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_1
sensor.solis_s6_eh1p_meter_total_active_energy_to_grid
sensor.solis_s6_eh1p_battery_charge_power
sensor.solis_s6_eh1p_battery_discharge_power
sensor.solis_s6_eh1p_household_load_power          ← den som faktiskt läses
sensor.aktuell_utetemperatur_bt1_40004
weather.smhi_home
sensor.batterycontrol_plan_now
sensor.batterycontrol_status
sensor.batterycontrol_load_forecast_now
sensor.batterycontrol_pv_forecast_now
```

**Solcast läses inte som sensor.** `forecast.py:81` läser
`/homeassistant/solcast_solar/solcast.json` via filsystemet (eller `/config/...` i add-on).
Det är en implementationsdetalj som påverkar uppsättningen — README säger "Solcast-sensorer".

### 2.3 Beroenden: `paramiko` är på väg ut men står kvar på två ställen

README rad 96 listar `paramiko` som mjukvaruberoende. Det stämmer **bara i utvecklingsläget**:
`ha_client.py` importerar det lazy inuti `SshHA`-klassen, med kommentaren *"add-on-imagen
behöver inte paramiko"*. Add-on-Dockerfilen installerar det inte heller.

**Men `forecast.py:16` har `import paramiko` på modulnivå — och använder det aldrig** (1
förekomst i filen: importen). Det är en död import som ändå kraschar hela prognossteget i en
miljö utan paramiko. Värt att städa oavsett README.

---

## 3. Kapitel 1–3 mot koden: vad är implementerat, bortlyft eller bortglömt?

### 3.1 Bortlyft sedan v1 — men beskrivs som aktivt

Dessa står i README som om de körs. De finns i `optimizer.py` (v1), **inte i `optimizer_v2.py`**:

| README | v1 | v2 |
|---|---|---|
| **Laddtrappan 0–4 / 4–6 / 6–8 / 8–10 / 10+ kW med 0 / +8 / +22 / +50 öre / +100 %** (rad 31–36, 149) | `tiers = [...]` med `penalty_type: fixed`/`relative` | **Fem jämna 2 kW-steg, påslag 0,04 / 0,12 / 0,20 / 0,28 / 0,36 kr/kWh.** Inget relativt påslag, ingen 10+ kW-nivå (taket är 10 kW). |
| **`c_hysteresis` = 5 öre rabatt i t=0** (rad 37, 161) | `c_hysteresis = 0.05`, `h_disc` i t=0 | **Finns inte.** Ingen kontinuitetsrabatt alls. |
| **`c_switch` = 3 öre ändring** (rad 38, 162) | `c_switch = 0.03` | **0,02 kr/kWh** (`c[b_idx + 4] = 0.02`). |
| **Dynamisk reservhantering: 1,0 kWh fram till 19:00, sedan fritt** (rad 40–42, 166) | `if is_effekttariff_day(dt_k) and 7 <= dt_k.hour < 19: b_ub.append(soc_now_kwh - 1.0)` | **Finns inte.** v2 har bara hälsogolvet 3,75 kWh. |
| **Huvudsäkringsskydd 14 kW** (rad 43) | Hård rad 14 kW | **13,5 kW** (`MAX_GRID_HOUR_KW`), ingen 14 kW-rad. |

Detta är den största avvikelsen: **fyra av raderna i §1.2 beskriver en optimeringsmotor som
inte längre körs.** Om README ska beskriva produktionen måste de antingen skrivas om till v2:s
faktiska parametrar eller flyttas till historiken i §5 (där de redan delvis finns).

### 3.2 Implementerat men inte nämnt någonstans

Allt detta körs och påverkar resultatet, men saknas i README:

| Modul / mekanism | Vad den gör | Varför den hör hemma i dokumentationen |
|---|---|---|
| **`peak_tracker.py`** | Läser P1/P2/P3 från HA, beräknar senaste kvartens medeleffekt, uppdaterar månadstopparna när en avslutad tariff­timme på nytt dygn slår en befintlig topp, beräknar dagens P_max | **Ändrar optimerarens tröskel varje kvart.** Utan den är P3 hårdkodad. |
| **`rescue_hour.py`** | Läser nätmätarens historik, integrerar upplupen import/export, räknar fram `E_nu` och matar LP:n | Är den mekanism som gör "rädda timmen" verklig i stället för en loggrad. |
| **`ev_block.py`** | Tidsförankrad EV-bokning; behovet krymper men fönstret flyttar sig inte | Löser att blocket annars "gled framför sig" varje kvart. |
| **Kvartstaket som *risktak*** | `E_q ≤ p_thresh · 0,25` | **Inte en tariffregel** — ett robusthetstak mot prognosfel, Solis som inte levererar, huvudsäkringen och enpunktsfel. Se `GLOBAL-OPTIMERING-MODELL.md` §4.8. README beskriver det inte alls, vilket gör att det ser ut som en artefakt. |
| **`E_nu` i timmedeleffektsraderna** | Pågående klocktimme mäts mot 1,0 h med redan förbrukad energi i högerledet | Ny sedan 2026-11-13. Utan den blir taket fyra gånger för högt vid `:45`. |
| **`P_max` i tröskeln** | En redan tagen topp är sjunket pris | Förklarar varför optimeraren inte "kämpar" mot ett tak den redan spräckt. |
| **EWMA-bias ±1,5 kW, α = 0,20** | Adaptiv korrigering av lastprognosen | Kapitel 1 nämner "dynamisk felkorrigering (EWMA)" men inte att den är **begränsad** — begränsningen är det som gör den stabil. |
| **Värmekoefficient ~75 W/°C under 17 °C** | Parametrisk baslast | Konkret och verifierbart; gör prognosavsnittet trovärdigt. |
| **Terminalvärde = sista kända kvartpriset** | Inte dygnsmedel | Avgörande för att batteriet inte panikladdas före midnatt. Nämns inte. |
| **Ingen batteri→nät-export i v2** | LP:n har ingen sådan variabel | README säger "säljer aldrig energi ut på nätet" (rad 133) — korrekt! Men §1.1 säger "urladdning sker mot huslasten", vilket är samma sak. Konsekvent, bra. |
| **Slackvariabel per *dygn*, inte per timme** | `P_peak_excess` per tariffdag | En per timme skulle straffa samma överskridande 13 gånger. |
| **Add-on-paketeringen** (`batterycontrol/`, `repository.yaml`) | HA-add-on: Dockerfile, ingress-server på :8099, `paths.py` | Hela distributionsformen saknas i README. Kapitel 3 beskriver bara utvecklingsmiljön. |
| **`ha_client.py`-fabriken** | Supervisor-API i add-on, SSH i utveckling | Två driftsätt i samma kodbas — påverkar felsökning. |
| **`daily_profit.py`** | Dygnsvinst med I/E/D/C-differenser | Dashboardens siffror. |

### 3.3 Bortglömt i koden (inte bara i dokumentationen)

- **`forecast.py` importerar `paramiko` på modulnivå utan att använda det.** Kraschar i
  add-on-imagen, som inte har paketet.
- **`generate_web_v2.py` finns men anropas aldrig** (0 träffar utanför filen själv).
  `run_loop.py` importerar `generate_web`.
- **`sensor.solis_s6_eh1p_peak_max_usable_grid_power` sätts aldrig** — se 2.2.
- **`optimizer_v2.py` docstring säger 13 variabler per kvart**, koden har 11. Samma siffra
  som README. En gemensam källa till förvirring.

---

## 4. Sammanfattning — prioriterat

0. **`origin/main` är inte körbart** (avsnitt 1b): `paths.py`, `peak_tracker.py`,
   `batterycontrol/`, `repository.yaml` är ocommittade, och `hour_len_h`-buggen finns kvar i
   `origin/main`. Committa arbetsträdet eller pinna `REPO_REF` — före allt annat.
1. **§1.2 rad 31–42 beskriver v1:s optimeringsmotor.** Antingen skrivs de om till v2:s
   parametrar (5 × 2 kW, 0,04–0,36 kr, c_switch 0,02, ingen hysterese, ingen 1 kWh-reserv,
   13,5 kW) eller flyttas till historiken.
2. **EV-blocket: tre olika tal i samma dokument** (2 h i §1.1, 3 h i §5 v0.5.0, 2 h i §5
   v0.4.0). Koden gör 3 h × 5,5 kW = **16,5 kWh** för en order som heter "10 kWh".
3. **Kapitel 2 beskriver `optimizer.py`.** Produktionen är `optimizer_v2.py` med 11 variabler.
   Sju körande moduler saknas i diagrammet.
4. **Peak-shaving-registret påstås styras** — det görs inte.
5. **Strukturförslaget:** tre nivåer i kapitel 1 i stället för två (nyttor / mekanismer /
   implementationsdetaljer), och slå ihop `Effektavgiftsminimering` med `Dynamiskt effekttak`.
6. **Lägg till ett avsnitt om risktaket.** Det är den enda delen av styrningen som inte är
   ekonomisk, och utan förklaring ser den ut som en godtycklig begränsning.
