# Batterycontrol – Prediktiv och kostnadsoptimal batteristyrning

**Batterycontrol** är en Home Assistant-add-on för ekonomiskt optimerad styrning av ett system med hembatteri, solpaneler och elbilsladdare. Systemet minimerar två kostnader samtidigt: inköpspriset på spotmarknaden och nätägarens effektavgift (avgiftsmodell: månadens tre högsta timmedeleffekter). Optimeringen baseras på prognoser för solproduktion och för hushållförbrukning av el. Styrningen är prediktiv (MPC — Model Predictive Control): var 15:e minut löses ett linjärt optimeringsproblem över den kända pris- och prognoshorisonten, och planens första kvart verkställs mot en **Solis S6-EH3P** hybridväxelriktare och en **Charge Amps**-laddbox via Home Assistants `solis_modbus`- respektive `chargeamps`-integration.

---

## 1. Systembeskrivning och funktionalitet

Systemet optimerar batteriets laddning och urladdning i 15-minutersintervall över en **dynamisk planeringshorisont** (upp till 36 timmar beroende på känd Nord-Pool-data, anpassat till midnatt idag före kl 13:00 och midnatt imorgon efter kl 13:00 med terminalvärde). Det körs cykliskt var 15:e minut och utför följande huvudfunktioner:

### 1.1 Huvudfunktioner
<!- Editorial comment: Urladdnings-delen av denna funktion 1.1.1 är inte implementerad. Den står på TODO-listan.
-->
#### 1.1.1 Spotprisarbitrage
Laddar batteriet när spotpriset är lågt, och laddar ur när spotpriset är högt. Urladdning sker mot huslasten och även mot nätet om det är lönsamt. Vid beräkningen om laddning/urladdning är lönsam och skall genomföras tas hänsyn till batteriets slitagekostnad och verkningsgrad för laddning/urladdning.
#### 1.1.2 Effektavgiftsminimering
Optimerar laddning och urladdning mot nätägarens (Göteborg Energi) prismodell för effektavgift. Prismodellen: priset beräknas på medelvärdet av de tre högsta tim-medel-effekt-topperna under effekttariff-fönstret, på olika dygn, under 1 månad. Effekttariffönstert är 7:00 - 20:00 helgfria vardagar mellan 1 november och sista mars.
#### 1.1.3 Mjuk och sammanhängande batteriladdning
Begränsar förluster och slitage på batteriet. Detta görs genom att sänka toppeffekter och hålla nere antalet på/av cykler vid laddning av batteriet. Denna funktion ger en avvikelse från den absolut optimala planen för batteriladdning, och medför alltså en (marginellt) minskad vinst.
#### 1.1.4 Samplanerad billaddning (16 kWh-beställning)
Erbjuder bokning av pris-optimal billaddning under kommande natt. Laddning bokas via en knapp i Home Assistant, och LP-optimeraren identifierar då det billigaste sammanhängande tidblocket, och laddar sedan bilen när tiden är inne.
#### 1.1.5 Huvudsäkringsskydd
Förhindrar att timmedeleffekten från nätet överskrider abonnemangstaket på 14 kW. 

### 1.2 Stödjande funktioner
#### 1.2.1 Adaptiv last- och produktionsprognos
Optimeraren bygger en rullande prognos (upp till ca 35h horisont) för hushållets elbehov och solproduktion:
  - Solprognos via Solcast.
  - Värmebehovsmodellering kopplad mot SMHI:s temperaturprognos och husets värmepumpskoefficienter (NIBE BT1).
  - Dynamisk felkorrigering (EWMA) mot momentant uppmätt huslast för att snabbt fånga avvikelser.

#### 1.2.2 Säsongsgolv för effekttak
Förhindrar att optimeraren cyklar hembatteriet i onödan på grund av orealistiskt lågt målvärde för effektavgiftsminimeringen. Optimeraren försöker hela tiden "inte gå över tidigare effekttopp", det så kallade taket. Med detta golv ändrar optimerarens strategi till att inte gå över den största av "förväntad topp för månaden (=golvet för taket) eller tidigare effekttopp". Taket har alltså ett hårt nedre golv på 1,30 kW (nov, feb, mar) och 1,60 kW (dec, jan).
  
#### 1.2.3 Tim-medeleffekt-optimering
Under effekt-tariff-tid läser optimeraren av importerad energi under innevarande timma och anpassar planen så att medel-effekten under hela timmen skall hållas under måltaket.

#### 1.2.4 Växlingsdämpning
En linjär straffterm mellan på varandra följande kvartar binder samman laddningen till stabila, sammanhängande tidsfönster.

---

### 1.3 Implementationsdetaljer 
<!- Editorial comment: Needs update after next code-update.
-->
#### 1.3.1 Dynamiskt effekttak
Minimerar effektavgiften genom att begränsa nätimportens toppeffekt under högpristid\. En brytpunkt beräknas per dygn som det högsta av tre värden: ett säsongsgolv (1,6 kW dec–jan, 1,3 kW nov/feb/mar, annars 0), månadens tredje högsta timmedeleffekt P₃, och dagens hittills högsta timmedeleffekt. P₃ är den verkliga kostnadströskeln — toppar under den påverkar inte avgiften. Import under brytpunkten är avgiftsfri; däröver kostar varje överskjutande kW 45 kr, vilket optimeraren väger mot batterislitage. Effekttaket är aktivt under effekttariff-tider (07:00–20:00 helgfria vardagar 1 nov–31 mars); utanför dessa tider gäller enbart det fasta timtaket på 13,5 kW.

#### 1.3.2 Fri spotoptimering under taket
Inom ramen för $P_{\text{dag}}$ råder verkligt spotpris utan artificiella påslag, vilket ger frihet för spotarbitrage och optimal lastfördelning.

#### 1.3.3 Fri Self-Use dagtid (Parering av transienter)
Under effekttariff (07:00–20:00) hålls `backup_soc` strikt på 15 % basgolv. Växelriktaren tillåts därmed parera momentana huslaster (kaffebryggare, ugn, micro) via Self-Use så att korta laster aldrig slår igenom som nättoppar.

#### 1.3.4 Solis-anpassad laddreglering
I stället för on/off-pulser med maxeffekt fördelas nödvändig laddenergi jämnt över hela 15-minuterskvarten genom dynamisk beräkning och rampning av strömbörvärdet (A) baserat på batterispänning.

#### 1.3.5 Stegvis progressiv prissättning av baserat på laddeffekt (5 trappsteg)
För att undvika att optimeraren rusar till 10 kW för marginella prisdifferenser modelleras laddeffekten till batteriet som en konvex styckvis linjär funktion. Intervallet 0–10 kW delas i fem steg om 2 kW, med ett stigande marginellt prispåslag på 4/12/20/28/36 öre/kWh. Påslaget är marginellt: det gäller energin *inom* respektive steg, så en kvart på 5 kW betalar 2 + 6 + 5 = 13 öre totalt, dvs 10,4 öre/kWh i genomsnitt.

#### 1.3.6 Urladdningsspärr via `backup_soc`
När optimeraren finner det mest lönsamt att spara lagrad energi till senare dyrare timmar (eller inför morgonens effekttaxa) spärras växelriktarens Self-Use autonomt genom att sätta `backup_soc` till aktuell SOC. När urladdning planeras sänks golvet till normal basreserv (15 %).

<!- Editorial comment
Denna funktion, 1.3.7, är inte implementerad. Behåller punkten på TODO-listan, för redesign och implementering.
-->
#### 1.3.7 Dynamisk reservhantering
Fram till kl 19:00 hålls minst 1,0 kWh (ca 5 % SOC) i reserv för att parera transienta hushållstoppar (1,0 kWh under en kvart motsvarar 4 kW kontinuerlig marginal). Mellan kl 19:00 och 20:00 släpps reserven fri ner mot batteriets hårdvarugolv (0 kWh användbart).

#### 1.3.8 Strikt lokal tidszonshantering (`Europe/Stockholm`):**
Alla tidsstämplar, kvartsintervall och timvillkor (07:00–20:00) hanteras explicit i svensk lokal tid (CET/CEST), vilket garanterar exakt synkronisering mot nätägarens tariffer och SMHI/Solcast/Nord Pool utan UTC-förskjutningar.

#### 1.3.9 Billaddning
Styr ChargeAmps Halo laddbox autonomt. Vid "bokning" identifieras det optimala 3-timmarsblocket (12 kvartar à 5,5 kW = 16,5 kWh) fram till kl 07:00. Elbilslasten läggs in i huslastprofilen så att hembatteriet samplaneras: nätimporten tar billaddningen under nattens djupaste prisdalar utan att hembatteriet töms in i bilen, samtidigt som hembatteriet säkerställs optimal laddning inför effekttariffstart kl 07:00.

## 2. Arkitektur och logik

Systemet är uppbyggt som en rullande MPC-loop uppdelad i tre samverkande lager:

```
                  [ Home Assistant REST API ]
                   /           |           \
     Solcast / SMHI / NIBE     |       Nordpool Spotpriser
                 |             |             |
                 v             v             v
       +------------------+    |    +-------------------+
       |   forecast.py    |    |    |  optimizer_v2.py  |
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
                       (TOU Slot 1, backup_soc)
```

1. **`src/forecast.py` (Prognosmotor):** Hämtar historisk baslast, väder- och solprognos, applicerar värmekoefficienter för utetemperatur och beräknar en adaptiv bias mot aktuell förbrukning. Skickar prognosvektorer till Home Assistant (anpassas dynamiskt av optimeraren till kända Nord Pool-kvartar).
2. **`src/optimizer_v2.py` (Global LP-lösare):** Formulerar ett linjärprogrammeringsproblem med **11 variabler per kvart** — nätimport till last (1), fem progressiva laddeffektsteg (5), energi till last / PV till last / PV till batteri / PV till nät (4) samt rampvariabeln `dP` (1). Lösaren (`scipy.optimize.linprog` med HiGHS-metoden) minimerar total nettokostnad under följande bivillkor:
   - lastbalans och PV-balans per kvart,
   - SOC-dynamik inom 3,75–25,0 kWh (15–100 % av 25 kWh nominell kapacitet),
   - batterigränser (max 10 kW laddning, max 2,5 kWh urladdning per kvart),
   - timmedeleffekt mot nätet ≤ 13,5 kW (`MAX_GRID_HOUR_KW`) med slackvariabel prissatt till 90 kr/kW — raden är en prissättning, inte ett hårt tak (huset får alltid sin effekt; växelriktaren prioriterar self-use och tar resten från nätet),
   - tariffrad per tariffdag ≤ taket plus slackvariabeln `P_peak_excess` (45 kr/kW),
   - rampdämpning `dP ≥ |ΔE_batt|`, viktad 0,02 kr/kWh för att undvika hackig laddning,
   - terminalvärde `v_term` = sista kända kvartens spotpris, så att batteriet varken töms eller panikladdas vid horisontens slut.

   Det finns **ingen kontinuitetshysteres** i v2 — den enda utjämningen är ramptermen `dP`. **Horisonten T** är antalet kvartar med känd Nord Pool-pris: optimeraren begär upp till 192 kvartar (48 h) och bryter så snart prisdata upphör. I praktiken blir det ca **25–48 timmar beroende på när på dygnet körningen sker** (morgondagens priser publiceras runt kl 13). Lösningen tar ca **0,02 s**.
3. **`src/run_loop.py` (Exekveringsloop):** Körs var 15:e minut — i add-onen av en intern loop i `run.sh` (`sleep 900`), i utvecklingsmiljön av cron. Kontrollerar driftlägen (`batterycontrol_active` och `batterycontrol_dry_run`), beräknar strömbörvärden (A) ur planens kW och uppmätt batterispänning, sätter urladdningsspärr (`backup_soc`) och skickar styrkommandon till växelriktaren via Home Assistant.
4. **`src/generate_plot.py` (Visualisering):** Renderar `plan_chart.png` som visar spotpriser, effektflöden och planerad batteri-SOC över den dynamiska planeringshorisonten.
5. **Stödtjänster runt loopen:**
   - `src/peak_tracker.py` — spårar månadens tre högsta timmedeleffekter och dygnets hittills högsta topp. **Källan är `peak_state.json`** (i `/data` som add-on, i `web/` i utvecklingsmiljön); `input_number.batterycontrol_p1..p3` är en **spegling** som skrivs om när den avviker. En omstart eller omladdning av Home Assistant kan därför inte ändra styrningen — spegeln rättas vid nästa kvart.
   - `src/rescue_hour.py` — mäter redan förbrukad nätenergi under innevarande timme (`E_nu`). Det är den direkta indatan till timmedeleffektsraderna, så att en pågående timme mäts mot hela timmens medelvärde och inte bara mot de kvartar som återstår. Den separata "rädda timmen"-raden i LP:n är borttagen 2026-11-17 — se `GLOBAL-OPTIMERING-MODELL.md` §4.7.
   - `src/ev_block.py` — bokning och samplanering av elbilsladdning (3 timmar à 5,5 kW = 16,5 kWh före kl 07:00).
   - `src/paths.py` — sökvägsupplösning som gör samma kod körbar både som cron-jobb och som add-on (`/data` som skrivbar yta, `src/` read-only i imagen).
   - `src/ha_client.py` — två HA-backends: `SupervisorHA` (add-on, `SUPERVISOR_TOKEN`) och `SshHA` (utveckling, SSH till HA-värden), med `get_state` / `post_state` / `call_service` / `history` / `read_json_file`.
   - `src/daily_profit.py` — dygnsarkiv för intäkt/kostnad som dashboarden läser.
   - `src/generate_web.py` + `batterycontrol/rootfs/app/ingress_server.py` — genererar dashboarden och serverar den på port 8099 i sidopanelen; kopian i `/config/www/batterycontrol/` möjliggör även `/local/`-åtkomst.

---

## 3. Beroenden och systembegränsningar

Systemet är utformat för en specifik hård- och mjukvaruinfrastruktur:

### 3.1 Hårdvaruberoende
- **Växelriktare:** Enbart testat och verifierat mot **Solis hybridväxelriktare** (specifikt 3-fas HV-hybrid, Solis S6-EH3P15K-H).
  - Styrningen förutsätter att växelriktaren är konfigurerad i driftläget `Self-Use + TOU + Reserve/Backup` (`select.work_mode`).
  - Laddning sker uteslutande via **TOU Slot 1**: `switch.grid_time_of_use_charging_period_1` slås på, `time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_1` / `..._end_slot_1` sätts till aktuellt kvart och blockets slut, `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1` får den beräknade strömmen och `..._cut_off_soc_slot_1` sätts till 100 %.
  - **Urladdning kan bara spärras eller tillåtas, inte regleras.** Spärren sker via Backup SOC (`number.solis_s6_eh1p_backup_soc`, register 43024): ett golv lika med aktuell SOC blockerar Self-Use, och 15 % släpper den fri ner till batteriets basreserv. Regeln är binär — under effekttariffönstret (07–20 på tariffdagar), eller när planen vill ladda ur, sätts golvet till 15 %, annars till aktuell SOC. Det finns alltså ingen kanal för att beordra en viss urladdningseffekt: planens urladdningsbehov loggas och verkställs indirekt via golvet.
  - **Solis inbyggda Grid Peak-Shaving** (`number.solis_s6_eh1p_peak_max_usable_grid_power`, register 43488) är konfigurerad i växelriktaren och står på 13 000 W, men **skrivs aldrig av programvaran**. Importtaket i systemet är LP-raden `MAX_GRID_HOUR_KW = 13,5 kW`, inte registret — och den raden har sedan 2026-11-17 en slackvariabel (90 kr/kW), eftersom taket inte är fysiskt: huset får sin effekt oavsett vad planen säger. Den fysiska backstoppen är huvudsäkringen, 20 A × 3 × 230 V = 13,8 kW.
  - Ingen batteriexport sker: planen innehåller flödet PV→nät, men inget batteri→nät.

### 3.2 Mjukvaruberoenden
- **Python-miljö:** Python 3.10+ med `scipy`, `numpy`, `holidays` och `matplotlib` (grafiken). HTTP sker enbart via standardbibliotekets `urllib` — `requests` används inte. `pandas` och `pyyaml` installeras i add-on-imagen men importeras inte av koden.
- **Känd begränsning:** `paramiko` importeras på modulnivå i `src/forecast.py` och `src/optimizer.py` men installeras inte i add-on-imagen. Add-onen kan därför inte starta förrän den importen gjorts lazy (mönstret finns redan i `src/ha_client.py`, som importerar `paramiko` först när SSH-backenden faktiskt används). I utvecklingsmiljön, där SSH-backenden används, är `paramiko` installerat och fungerande.
- **Home Assistant:** Förutsätter en fungerande Home Assistant-instans med REST-API och följande entiteter:
  - *Priser:* `sensor.nordpool_predbat_import`, `sensor.nordpool_predbat_export`
  - *Sol & Väder:* Solcast läses ur filen `/homeassistant/solcast_solar/solcast.json` (inte ur en sensor), SMHI via tjänsteanropet `weather.get_forecasts` på `weather.smhi_home`, utetemperatur via `sensor.aktuell_utetemperatur_bt1_40004`
  - *Hus & Värmepump:* Huslast `sensor.solis_s6_eh1p_household_load_power`, NIBE utegivare BT1
  - *Batteristatus:* `sensor.solis_s6_eh1p_battery_soc`, `sensor.solis_s6_eh1p_battery_voltage`, `sensor.solis_s6_eh1p_battery_charge_power`, `sensor.solis_s6_eh1p_battery_discharge_power`
  - *Solis Modbus-styrning:* `number.solis_s6_eh1p_backup_soc`, `switch.grid_time_of_use_charging_period_1`, `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1`, `number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_1`, `time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_1`, `time.solis_s6_eh1p_grid_time_of_use_charge_end_slot_1`
  - *Plan- och prognossensorer (skrivs av systemet):* `sensor.batterycontrol_plan_now`, `sensor.batterycontrol_status`, `sensor.batterycontrol_load_forecast_now`, `sensor.batterycontrol_pv_forecast_now`
  - *Toppspårning:* `input_number.batterycontrol_p1`, `input_number.batterycontrol_p2`, `input_number.batterycontrol_p3` (spegling av `peak_state.json`, `initial: 0.0` — se `src/peak_tracker.py`)
  - *Styrflaggor & Beställningar:* `input_boolean.batterycontrol_active`, `input_boolean.batterycontrol_dry_run`, `input_boolean.ev_charge_order_10kwh`
  - *Mätare & Elbil:* Nätmätare `sensor.solis_s6_eh1p_meter_total_active_energy_from_grid` och `..._to_grid`, Elbilseffekt `sensor.ev_power` (i **kW**, inte W), Charge Amps-integrationen (`chargeamps.enable`, `chargeamps.disable`, `chargeamps.set_max_current`)

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

