# Batterycontrol – Prediktiv och kostnadsoptimal batteristyrning

**Batterycontrol** är ett system för prediktiv, rullande optimering (MPC – Model Predictive Control) av hembatteri mot dynamiska elpriser och effekttariffer. Systemet samverkar med **Home Assistant** och styr en **Solis hybridväxelriktare** via Modbus.

---

## 1. Systembeskrivning och funktionalitet

Systemet optimerar batteriets laddning och urladdning i 15-minutersintervall över en **dynamisk planeringshorisont** (upp till 36 timmar beroende på känd Nord Pool-data, anpassat till midnatt idag före kl 13:00 och midnatt imorgon efter kl 13:00 med terminalvärde). Det körs cykliskt var 15:e minut via cron och utför följande huvudfunktioner:

### 1.1 Huvudfunktioner
- **Spotprisarbitrage:** Laddar batteriet under lågprisperioder och urladdar under dyra timmar med hänsyn tagen till batteriets slitagekostnad och verkningsgrader vid laddning/urladdning ($\eta_{\text{chg}}=0.95, \eta_{\text{dis}}=0.95$).
- **Effekttaxeminimering (Göteborg Energi):** Optimerar automatiskt mot nätägarens effekttariff (135 kr/kW·månad baserat på medelvärdet av månadens tre högsta timtoppar på olika helgfria vardagar kl 07:00–20:00 under perioden 1 november – 31 mars).
  - **Dynamiskt effekttak ({\text{dag}}$):** Minimerar toppeffekten under högpristid genom att införa en dygnsoptimerad takvariabel {\text{dag}}$ med en skuggtaxa på 45 kr/kW (motsvarande 35/3$ kr/kW per månadstopp). Nödvändig nätimport smetas därmed ut helt platt över alla kvartar mellan 07:00 och 20:00.
  - **Fri spotoptimering under taket:** Inom ramen för {\text{dag}}$ råder verkligt spotpris utan artificiella påslag, vilket ger frihet för spotarbitrage och optimal lastfördelning.
- **Adaptiv last- och produktionsprognos:** Bygger en rullande prognos (upp till 48h horisont) för hushållets elbehov och solproduktion:
  - Solprognos via Solcast.
  - Värmebehovsmodellering kopplad mot SMHI:s temperaturprognos och husets värmepumpskoefficienter (NIBE BT1).
  - Dynamisk felkorrigering (EWMA) mot momentant uppmätt huslast för att snabbt fånga avvikelser.
- **Solis-anpassad laddreglering:** I stället för on/off-pulser med maxeffekt fördelas nödvändig laddenergi jämnt över hela 15-minuterskvarten genom dynamisk beräkning och rampning av strömbörvärdet (A) baserat på batterispänning.
- **Mjuk och sammanhängande laddning (Stegvis progressiv trappa, dygnsagnostisk med kontinuitetshysteres):** Laddning från elnätet optimeras för att hålla en lugn och jämn effekt i breda, sammanhängande block utan stötvisa spikar mot huvudsäkringen eller onödiga avbrott vid små prisfluktuationer:
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
  - *Styrflaggor:* `input_boolean.batterycontrol_active`, `input_boolean.batterycontrol_dry_run`

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

