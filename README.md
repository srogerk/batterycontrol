# Batterycontrol – Prediktiv och kostnadsoptimal batteristyrning

**Batterycontrol** är ett system för prediktiv, rullande optimering (MPC – Model Predictive Control) av hembatteri mot dynamiska elpriser och effekttariffer. Systemet samverkar med **Home Assistant** och styr en **Solis hybridväxelriktare** via Modbus.

---

## 1. Systembeskrivning och funktionalitet

Systemet optimerar batteriets laddning och urladdning i 15-minutersintervall över en **dynamisk planeringshorisont** (upp till 36 timmar beroende på känd Nord Pool-data, anpassat till midnatt idag före kl 13:00 och midnatt imorgon efter kl 13:00 med terminalvärde). Det körs cykliskt var 15:e minut via cron och utför följande huvudfunktioner:

### 1.1 Huvudfunktioner
- **Spotprisarbitrage:** Laddar batteriet under lågprisperioder och urladdar under dyra perioder. Urladdning sker mot hushållet, och ersätter alltså inköp av el med ett högre pris. Vid beräkningen om laddning/urladdning är lönsam och skall genomföras tas hänsyn tas till batteriets slitagekostnad och verkningsgrad vid laddning/urladdning.
- **Effektavgiftsminimering:** Optimerar laddning och urladdning mot nätägarens (Göteborg Energi) prismodell för effektavgift. Prismodellen är utformad som följer: Priset beräknas på medelvärdet av de tre högsta tim-medel-effekt-topperna under effekttariff-fönstret, på olika dygn, under 1 månad. Effekttariffönstert är 7 - 20 helgfria vardagar mellan 1 november och sista mars.
- **Mjuk och sammanhängande batteriladdning:** Strävar efter att hålla nere på-av frekvensen för laddning av batteriet genom att utföra laddningarna i sammanhängande block, och strävar efter att minska förluster och slitage på batteriet genom att sänka toppeffekter och ladda längre tid i stället. Denna funktion justerar den optimala planen för batteriladdning och urladdning, och medför alltså en kostnad pga utebliven vinst. 
- **Adaptiv last- och produktionsprognos:** Bygger en rullande prognos (upp till 48h horisont) för hushållets elbehov och solproduktion:
  - Solprognos via Solcast.
  - Värmebehovsmodellering kopplad mot SMHI:s temperaturprognos och husets värmepumpskoefficienter (NIBE BT1).
  - Dynamisk felkorrigering (EWMA) mot momentant uppmätt huslast för att snabbt fånga avvikelser.
- **Solis-anpassad laddreglering:** I stället för on/off-pulser med maxeffekt fördelas nödvändig laddenergi jämnt över hela 15-minutersintervall genom dynamisk beräkning och rampning av strömbörvärdet (A) baserat på batterispänning.

### 1.2 Stödjande funktioner
- **Dynamiskt effekttak ($P_{\text{dag}}$):** Minimerar effektavgiften genom att begränsa toppeffekten för nät-importen under högpristid. Detta görs genom att införa ett dygnsoptimerad takvariabel $P_{\text{dag}}$. Nödvändig nätimport smetas därmed ut helt platt över alla 15-mintersperioder mellan 07:00 och 20:00.
- **Säsongsgolv för effekttak:** Förhindrar att optimeraren cyklar hembatteriet i onödan på grund av orealistiskt lågt målvärde för effektavgiftsminimeringen. Optimeraren försöker hela tiden "inte gå över tidigare effekttopp", det så kallade taket. Med detta golv ändrar optimerarens strategi till att inte gå över den största av "förväntad topp för månaden (=golvet) eller tidigare effekttopp". Taket $P_{\text{dag}}$ har alltså ett hårt nedre golv på 1,30 kW (nov, feb, mar) och 1,60 kW (dec, jan).
- **Fri spotoptimering under taket:** Inom ramen för $P_{\text{dag}}$ råder verkligt spotpris utan artificiella påslag, vilket ger frihet för spotarbitrage och optimal lastfördelning.
- **Fri Self-Use dagtid (Parering av transienter):** Under effekttariff (07:00–20:00) hålls `backup_soc` strikt på 15 % basgolv. Växelriktaren tillåts därmed parera momentana huslaster (kaffebryggare, ugn, micro) via Self-Use så att korta laster aldrig slår igenom som nättoppar.
- **Rädda timmen-vakt:** I kvart 2, 3 och 4 av varje klocktimme under effekttariffen kontrolleras ackumulerad energi från nätmätaren (`sensor.solis_s6_eh1p_meter_total_active_energy_from_grid`). Om timmen riskerar att överskrida måltaket minskas tvingas urladdning fram för att skydda timmedelvärdet.
- **Solis-anpassad laddreglering:** I stället för on/off-pulser med maxeffekt fördelas nödvändig laddenergi jämnt över hela 15-minuterskvarten genom dynamisk beräkning och rampning av strömbörvärdet (A) baserat på batterispänning.
  - **Stegvis progressiv laddeffekt (5 trappsteg):** För att undvika att optimeraren rusar till 10 kW för marginella prisdifferenser modelleras laddeffekten som en konvex styckvis linjär funktion:
    - *Steg 1 (0–4 kW):* Mjuk basladdning utan extra påslag (0,00 kr/kWh).
    - *Steg 2 (4–6 kW):* Moderat laddeffekt (+0,08 kr/kWh påslag).
    - *Steg 3 (6–8 kW):* Högre laddeffekt (+0,22 kr/kWh påslag).
    - *Steg 4 (8–10 kW):* Hög laddeffekt (+0,50 kr/kWh påslag, reserveras för kraftiga prisdippar).
    - *Steg 5 (10+ kW):* Effektspik över 10 kW (+100 % påslag av spotpriset, d.v.s. dubbelt pris, vilket effektivt förhindrar nätspikar mot huvudsäkringen).
  - **Kontinuitetshysteres (`c_hysteresis` = 5 öre/kWh):** Om laddning redan pågick föregående kvart ges innevarande kvart ($t=0$) en rabatt på 5 öre/kWh. Detta motverkar fladder ('chatter') och hindrar omplaneringen från att avbryta ett påbörjat laddblock för små marginella prisskillnader.
  - **Växlingsdämpning (`c_switch` = 3 öre/kWh ändring):** En linjär straffterm på $|P_t - P_{t-1}|$ mellan på varandra följande kvartar binder samman laddningen till stabila, sammanhängande tidsfönster.
- **Urladdningsspärr via `backup_soc`:** När optimeraren finner det mest lönsamt att spara lagrad energi till senare dyrare timmar (eller inför morgonens effekttaxa) spärras växelriktarens Self-Use autonomt genom att sätta `backup_soc` till aktuell SOC. När urladdning planeras sänks golvet till normal basreserv (15 %).
- **Dynamisk reservhantering:**
  - Fram till kl 19:00 hålls minst 1,0 kWh (ca 5 % SOC) i reserv för att parera transienta hushållstoppar (1,0 kWh under en kvart motsvarar 4 kW kontinuerlig marginal).
  - Mellan kl 19:00 och 20:00 släpps reserven fri ner mot batteriets hårdvarugolv (0 kWh användbart).
- **Huvudsäkringsskydd:** Strikt bivillkor i optimeraren som förhindrar att timmedeleffekten från nätet någonsin överskrider abonnemangstaket på 14 kW.
- **Samplanerad elbilsladdning (10 kWh-beställning):**
  - Styrs via en ren knapp i Home Assistant (`input_boolean.ev_charge_order_10kwh`, "Ladda elbil 10 kWh innan 07:00").
  - Vid aktivering identifierar LP-optimeraren det globalt billigaste sammanhängande 2-timmarsblocket (8 kvartar à 5,5 kW = 11 kWh) fram till kl 07:00.
  - Elbilslasten läggs in i huslastprofilen så att hembatteriet samplaneras: nätimporten tar billaddningen under nattens djupaste prisdalar utan att hembatteriet töms in i bilen, samtidigt som hembatteriet säkerställs full laddning (100 %) inför effekttariffstart kl 07:00.
  - Styr Charge Amps Halo laddbox autonomt (`chargeamps.enable` under blocket, `chargeamps.disable` övrig tid och alltid under aktiv effekttariff).
- **Strikt lokal tidszonshantering (`Europe/Stockholm`):**
  - Alla tidsstämplar, kvartsintervall och timvillkor (07:00–20:00) hanteras explicit i svensk lokal tid (CET/CEST), vilket garanterar exakt synkronisering mot nätägarens tariffer och SMHI/Solcast/Nord Pool utan UTC-förskjutningar.

---

## 2. Arkitektur och logik

Systemet är uppbyggt som en rullande MPC-loop uppdelad i tre samverkande lager:

```
                  [ Home Assistant REST API ]
                   /           |           \
     Solcast / SMHI / NIBE     |       Nordpool Spotpriser
                 |             |             |
                 v             v             v
       +------------------+    |    +-------------------+
       |   forecast.py    |    |    |   optimizer.py    |
       | Adaptiv last- &  |    |    | Global dynamisk   |
       | solprognos       |--->|--->| LP (SciPy HiGHS)  |
       +------------------+    |    +-------------------+
                               |              |
                               | (Börvärden)  v
                      +------------------+
                      |   run_loop.py    |
                      | Kvartsvis styr-  |
                      | & säkerhetslogik |
                      +------------------+
                               |
                               v
                     [ Solis Modbus / HA ]
             (TOU Slot 1, backup_soc, peak-shaving)
```

1. **`src/forecast.py` (Prognosmotor):** Hämtar historisk baslast, väder- och solprognos, applicerar värmekoefficienter för utetemperatur och beräknar en adaptiv bias mot aktuell förbrukning. Skickar prognosvektorer till Home Assistant (anpassas dynamiskt av optimeraren till kända Nord Pool-timmar).
2. **`src/optimizer.py` (Global LP-lösare):** Formulerar ett linjärprogrammeringsproblem (LP) med dynamisk horisont T kvartar (upp till 36 timmar kända Nord Pool-priser) och 13 variabler per kvart (inklusive 5 progressiva laddeffektsteg och rampningsvariabler). Lösaren (`scipy.optimize.linprog` med HiGHS-metoden) minimerar total nettokostnad under givna bivillkor (SOC-dynamik, batterigränser, effekttak, säkringsgräns, kontinuitetshysteres och terminalvärde). Lösningen tar ca 0,05 s.
3. **`src/run_loop.py` (Exekveringsloop):** Anropas varje kvart via systemets scheduler (cron). Kontrollerar driftlägen (`batterycontrol_active` och `batterycontrol_dry_run`), beräknar strömbörvärden (A), sätter urladdningsspärr (`backup_soc`) och skickar styrkommandon till växelriktaren via Home Assistant.
4. **`src/generate_plot.py` (Visualisering):** Renderar `plan_chart.png` som visar spotpriser, effektflöden och planerad batteri-SOC för den dynamiska planeringshorisonten (11–36 timmar).

---

## 3. Beroenden och systembegränsningar

Systemet är utformat för en specifik hård- och mjukvaruinfrastruktur:

### 3.1 Hårdvaruberoende
- **Växelriktare:** Enbart testat och verifierat mot **Solis hybridväxelriktare** (specifikt 3-fas HV-hybrid, Solis S6-EH3P15K-H).
  - Styrningen förutsätter att växelriktaren är konfigurerad i driftläget `Self-Use + TOU + Reserve/Backup`.
  - Laddning sker via TOU Slot 1 (`grid_time_of_use_charge_*`).
  - Urladdningsspärr styrs via registret för Backup SOC (`number.solis_s6_eh1p_backup_soc`, register 43024).
  - Hårt nätimporttak styrs via Solis inbyggda Grid Peak-Shaving (`number.solis_s6_eh1p_peak_max_usable_grid_power`, register 43488).

### 3.2 Mjukvaruberoenden
- **Python-miljö:** Python 3.10+ med `scipy`, `numpy`, `paramiko`, `holidays`, `requests`/`urllib`.
- **Home Assistant:** Förutsätter en fungerande Home Assistant-instans med REST-API och följande entiteter:
  - *Priser:* `sensor.nordpool_predbat_import`, `sensor.nordpool_predbat_export`
  - *Sol & Väder:* Solcast-sensorer, SMHI utetemperaturprognos
  - *Hus & Värmepump:* Huslast (`sensor.solis_s6_eh1p_house_load` eller motsvarande), NIBE utegivare BT1
  - *Batteristatus:* `sensor.solis_s6_eh1p_battery_soc`, `sensor.solis_s6_eh1p_battery_voltage`, `sensor.solis_s6_eh1p_battery_power`
  - *Solis Modbus-styrning:* `number.solis_s6_eh1p_backup_soc`, `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1`, `time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_1`, `time.solis_s6_eh1p_grid_time_of_use_charge_end_slot_1`, `number.solis_s6_eh1p_peak_max_usable_grid_power`
  - *Styrflaggor & Beställningar:* `input_boolean.batterycontrol_active`, `input_boolean.batterycontrol_dry_run`, `input_boolean.ev_charge_order_10kwh`
  - *Mätare & Elbil:* Nätmätare (`sensor.solis_s6_eh1p_meter_total_active_energy_from_grid`), Elbilseffekt (`sensor.ev_power` i kW), Charge Amps integration (`chargeamps.enable`, `chargeamps.disable`)

---

## 4. Utvecklings- och versionshanteringsstrategi

För att kombinera stabil produktion med kontinuerlig vidareutveckling tillämpas följande gren- och versionsstrategi:

1. **`main` (Produktionsgren):**
   - Innehåller den stabila baslinje som är driftsatt och körs i skarp miljö.
   - Endast godkända, testade och verifierade ändringar slås ihop till `main`.
   - Releaser och baselines taggas här (`v0.1.0`, `v0.2.0`, osv.).
2. **`maintenance` / Hotfix-grenar (`fix/...`):**
   - Skapas vid behov utifrån aktuell produktionsrelease för snabba felrättningar i skarp drift utan att dra med pågående experiment.
3. **Funktionsgrenar (`feature/...` eller testgrenar):**
   - Nya funktioner (t.ex. dynamisk samoptimering av månadens 3 toppar, styrning av elbilsladdning, etc.) utvecklas och testas i separata grenar.
   - Körs i skuggläge (`dry-run`) innan sammanslagning med `main`.

---

## 5. Projektstatus & Historik

- **2026-11-09 (v0.5.1 / Justerat terminalvärde mot nattpris vid horisontslut):**
  - **Reviderat terminalvärde i `optimizer_v2.py`:** Ersatte dygnsmedelvärdet (`mean(p_in)`) med sista kända kvartens elpris vid midnatt (`p_in[-1]`). 
  - **Eliminerat artificiell övervärdering:** Förhindrar att batteriet sparar energi mot dyr dagel eller tvingas panikladda mot 100 % före midnatt.
  - **Fullt batteriutnyttjande och mjuk landning:** Batteriet täcker hushållslasten fullt ut dagtid (nätimport minimeras), landar mjukt på 15 % basreserv kl 21:00 och håller sig lugnt utan stressladdning fram till nattens lägsta spotpriser.
- **2026-11-08 (v0.5.0 / Optimizer v2 – HiGHS LP, 25 kWh, Mjuk laddning, Ren nät-EV & Effekttariff):**
  - **Optimizer v2 med HiGHS LP (`optimizer_v2.py`):** Komplett ny optimeringsmotor baserad på `scipy.optimize.linprog` (HiGHS) med dynamisk horisont (upp till 36+ h beroende på Nord Pool morgondagspriser). Löser hela problemet på under 0,1 sekund.
  - **Uppdaterad batterikapacitet (25 kWh nominell):** Konfigurerad för 25 kWh nominell kapacitet med 85 % användbart fönster (15 % – 100 %, d.v.s. 3,75 kWh till 25,00 kWh).
  - **Ingen batteriexport ($E_{\text{batt}\to\text{grid}} = 0$):** Batteriet täcker enbart husets egenförbrukning (Self-Use) och säljer aldrig energi ut på nätet.
  - **Effekttariff 1 nov – 31 mars (07:00–20:00 vardagar):** Optimerar strikt mot effekttaxan med säsongsgolv $P_{\text{tak}} = 1,30\text{ kW}$ (nov, feb, mar) och $1,60\text{ kW}$ (dec, jan), kombinerat med historiska toppar ($P_3$). Straffavgift 45 kr/kW per toppdag.
  - **Mjuk/progressiv laddning:** Styckvis linjär konvex approximation av laddkostnaden med 0,04 kr/kW i 5 nivåer (0–2, 2–4, 4–6, 6–8, 8–10 kW) kombinerat med växlingsdämpning mellan intilliggande kvartar. Hindrar ryckiga laddspikar och smetar ut nattladdningen jämnt.
  - **Elbilsladdning som ren nätimport (22:00–07:00):** 3 sammanhängande timmar à 5,5 kW allokeras automatiskt under nattens billigaste timmar utanför effekttariffen. Bilen laddas till 100 % från nätet utan att dränera hembatteriet ($P_{\text{batt}\to\text{load}} = 0$ under laddblocket).
  - **Korrigerat terminalvärde:** Både nätladdning och urladdning balanserar symmetriskt mot terminalvärdet $V(\text{SOC}_T) = v_{\text{term}} \cdot \text{SOC}_T$, vilket garanterar att batteriet fylls på vid prisdalar inför dyra timmar.
  - **Driftsättning och bakåtkompatibilitet:** `src/run_loop.py` uppdaterad till Optimizer v2. Tidigare v1 sparad som `src/run_loop_v1.py` för snabb återgång vid behov.
- **2026-11-03 (v0.4.0 / Samplanerad elbilsladdning, Tidszonssynk & Rädda timmen):**
  - **Samplanerad elbilsladdning (10 kWh innan 07:00):** Införde stöd för `input_boolean.ev_charge_order_10kwh`. Optimeraren schemalägger ett sammanhängande 2-timmarsblock (8 kvartar à 5,5 kW) under nattens billigaste timmar före kl 07:00. Hembatteriet samplaneras så att det inte laddar ur till bilen, och Charge Amps styrs autonomt (`enable`/`disable`).
  - **Rättat enhetsfel för `sensor.ev_power`:** Identifierat att sensorn rapporterar i kW (inte W); åtgärdat subtraktion i `forecast.py` så att elbilsladdning inte förvanskar husets baslastprognos.
  - **Tidszonshantering (`Europe/Stockholm`):** Ersatte UTC-baserade kvartsstämplar i `forecast.py` och `optimizer.py` med `zoneinfo.ZoneInfo("Europe/Stockholm")`. Eliminerat 1-timmes tidszonsglapp där effekttariffen felaktigt utvärderades mot 08:00–21:00 svensk tid.
  - **Fri Self-Use under effekttariff:** `backup_soc` låses inte längre till aktuell laddnivå under 07:00–20:00 vardagar. Solis tillåts parera momentana huslaster fritt ner till 15 % basgolv.
  - **Rädda timmen-vakt:** Kvartsvis avläsning av nätmätaren `sensor.solis_s6_eh1p_meter_total_active_energy_from_grid` i `run_loop.py` för att övervaka och skydda timmedelvärdet mot överskridande av måltaket (1,30 kW).
- **2026-11-02 (v0.3.2 / Säsongsgolv för effekttak):**
  - **Säsongsgolv $P_{\text{floor}}$:** Implementerade `get_p_dag_floor_kw()` med 1,30 kW (nov, feb, mar) och 1,60 kW (dec, jan) för att förhindra onödigt battericyklande på baslast.
  - Dokumenterade marginaloptimeringslogiken i `EFFEKTAVGIFT-GOLV-LOGIK.md`.
- **2026-11-01 (v0.3.1 / Stegvis progressiv laddtrappa):**
  - **Stegvis progressiv laddeffekt i 5 nivåer:** Ersatte 2-stegsmodellen (5 kW + 5 kW @ 15 öre) med en konvex styckvis linjär trappa: 0–4 kW (0 öre), 4–6 kW (+8 öre), 6–8 kW (+22 öre), 8–10 kW (+50 öre) och 10+ kW (+100 % av spotpriset / dubbelt pris).
  - **Kapar nätspikar och eliminerar laddglapp:** Nätspiken nattetid sjunker från 13,1 kW ner till 9,3 kW (god säkerhetsmarginal till 14 kW huvudsäkring), och korta avbrott runt midnatt elimineras till förmån för jämn 4 kW-laddning. Vinstpåverkan: endast ca 1 kr över 32 h.
  - **Konceptuell QP-modell parkerad:** Den teoretiska kontinuerliga kvadratiska dämpningsmodellen ($P^2$) dokumenterad och parkerad på grenen `concept/quadratic-progression-qp`.
- **2026-11-01 (v0.3.0 / Skarp Modbus-driftsättning & Dynamiskt effekttak):**
  - **Skarp Modbus-styrning driftsatt:**  styr Solis hybridväxelriktare live via Home Assistant REST API (, strömbörvärden i Ampere,  som urladdningsspärr mot Self-Use). Verifierat skarpt med ~5.3 kW nätladdning.
  - **Dynamiskt effekttak ({\text{dag}}$):** Ersatte stelt tvång på 100 % SOC kl 07:00 med ekonomiskt grundad LP-målfunktion (45 kr/kW på {\text{dag}}$ under vardagar 07:00–20:00 nov–mar). Tarifferar nätimport så att nattladdning före 07:00 sker naturligt och importen under dagen plattas ut helt.
  - **Integrerad webb-dashboard:**  regenererar och synkar automatiskt webb-dashboarden till  efter varje kvartskörning.
  - **Aktiv styrning påslagen:**  och .
- **2026-10-31 (v0.2.1 / feature/smooth-charging):**
  - Mjuk och sammanhängande laddning med dygnsagnostisk formulering (löser 15-minuters fragmentering).
  - Utökade LP-modellen till 10 variabler per kvart med linjära rampningsvariabler ($R^+_t, R^-_t$).
  - Justerade tröskelpremie för laddeffekt $>5\text{ kW}$ till 15 öre/kWh (`c_extra_penalty = 0.15`).
  - Införde kontinuitetshysteres (`c_hysteresis = 0.05 kr/kWh`) baserat på föregående kvarts faktiska laddstatus (`prev_charge_kw`).
  - Införde växlingsdämpning (`c_switch = 0.03 kr/kWh ändring`) för att binda samman laddningen i stabila tidsblock.
- **2026-10-30:**
  - Skapat GitHub-repository (`srogerk/batterycontrol`) och taggat första officiella baseline: **`v0.1.0`**.
  - Infört spärrfunktion för Self-Use via `backup_soc` i `run_loop.py`.
  - Infört uppdaterade reservvillkor: 1,0 kWh reserv fram till kl 19:00 under effekttariff-dagar, därefter fri förbrukning ner mot 0 kWh.
  - Implementerat tjänsteanrop (`call_service`) via Home Assistant API.
- **2026-10-29:**
  - Komplett driftsättning av LP-optimerare (HiGHS) med kalenderstöd för effekttariff (`holidays.Sweden()`).
  - Begränsat maximal laddeffekt till 10 kW (hårdvarugräns för laddning).
  - Infört dynamisk beräkning av laddström (A) för att smeta ut laddeffekt jämnt över hela kvartar.
- **2026-10-27:**
  - Analys av historisk timdata 2025 och specifikation av effekttariffregler (135 kr/kW·månad).
  - Verifierat att Solis inbyggda peak-shaving (13 kW) klampar importen på ~12,85 kW även vid samtidig TOU-laddning.
  - Förbjudit RC-force som laddväg p.g.a. risk för okontrollerad spikurladdning.

