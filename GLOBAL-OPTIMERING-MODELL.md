# Global optimeringsmodell för batteristyrning (48h MPC)

*Dokumenterad 2026-10-28 utifrån designunderlag överenskommet mellan Roger och AI-partner.*

---

## 1. Sammanfattning & Grundfilosofi

Den globala metoden ersätter lokala heuristiker (som "backmetoden" eller fasta tidsfönsterregler) med en **Model Predictive Control (MPC)**-formulering:

* **Horisont:** 48 timmar framåt i tiden.
* **Tidsupplösning:** 15 minuter ($T = 192$ tidsintervall).
* **Rullande omplanering:** Körs **varje kvart** med uppdaterade mätvärden (faktisk SOC) och uppdaterade prognoser för elpris, huslast och solproduktion.
* **Princip:** Istället för att konstruera explicita specialregler för varje lokal pristopp, prisdal eller solväder, formuleras alla energiflöden och fysikaliska begränsningar matematiskt. Den ekonomiskt optimala laddnings- och urladdningsprofilen uppstår då som en direkt följd av den globala optimeringen.

---

## 2. Grundförutsättningar & Parametrar

| Parameter | Värde / Princip |
|---|---|
| **Planeringshorisont** | 48 timmar |
| **Tidsupplösning** | 15 minuter |
| **Antal tidsintervall ($T$)** | 192 kvartar |
| **Omplaneringsfrekvens** | Varje kvart ($t = 0$) |
| **Användbar batterikapacitet** | 20 kWh ($SOC \in [0, 20]$ kWh) |
| **Maximal effekt (ladd/urladd)** | 15 kW |
| **Maximal energi per kvart** | 3,75 kWh (före förluster) |
| **Maximal nätimport (avtalsgräns)** | 14 kW i genomsnitt per fast klocktimme ($\sum_{t \in h} E^{\mathrm{in}}_t \le 14$ kWh) |
| **Solproduktion** | Topp ca 8 kW, upp till ~70 kWh/dag (prognos per kvart) |
| **Husförbrukning** | Prognos per kvart |
| **Prisdata** | Spotpris per kvart + marginalpriser för import/export |
| **Ingående SOC ($SOC_0$)** | Faktiskt uppmätt värde vid planens start |
| **Utgående SOC ($SOC_{\mathrm{slut}}$)** | Fri variabel, värderas via terminalfunktion $V(SOC_{\mathrm{slut}})$ |

---

## 3. Modell B: Separata energiflöden per kvart

För att garantera energibalans och förhindra dubbelräkning eller fiktiv rundgång modelleras flödena explicit för varje kvart $t \in [0, 191]$:

### Variabler per kvart $t$:
* $E^{\mathrm{in}}_t \ge 0$: Energi köpt från nätet.
* $E^{\mathrm{ut}}_t \ge 0$: Energi exporterad till nätet.
* $E^{\mathrm{pv}\to\mathrm{hus}}_t \ge 0$: Solenergi som förbrukas direkt i huset.
* $E^{\mathrm{pv}\to\mathrm{batt}}_t \ge 0$: Solenergi som laddar batteriet.
* $E^{\mathrm{pv}\to\mathrm{nät}}_t \ge 0$: Solenergi som exporteras till nätet.
* $E^{\mathrm{nät}\to\mathrm{batt}}_t \ge 0$: Nätenergi som laddar batteriet.
* $E^{\mathrm{batt}\to\mathrm{hus}}_t \ge 0$: Batterienergi som täcker huslast.
* $E^{\mathrm{batt}\to\mathrm{nät}}_t \ge 0$: Batterienergi som exporteras (normalt 0 om arbitrage ej medger det).
* $SOC_t$: Batteriets energilager vid början av kvart $t$.

### Balansekvationer:
1. **Solbalans:**
   $$E^{\mathrm{pv}}_t = E^{\mathrm{pv}\to\mathrm{hus}}_t + E^{\mathrm{pv}\to\mathrm{batt}}_t + E^{\mathrm{pv}\to\mathrm{nät}}_t + E^{\mathrm{curtail}}_t$$
2. **Huslastbalans:**
   $$E^{\mathrm{hus}}_t = E^{\mathrm{pv}\to\mathrm{hus}}_t + E^{\mathrm{batt}\to\mathrm{hus}}_t + E^{\mathrm{nät}\to\mathrm{hus}}_t$$
3. **Nätimport:**
   $$E^{\mathrm{in}}_t = E^{\mathrm{nät}\to\mathrm{hus}}_t + E^{\mathrm{nät}\to\mathrm{batt}}_t$$
4. **Nätexport:**
   $$E^{\mathrm{ut}}_t = E^{\mathrm{pv}\to\mathrm{nät}}_t + E^{\mathrm{batt}\to\mathrm{nät}}_t$$
5. **Batteri in/ut:**
   $$E^{\mathrm{ladd},t} = E^{\mathrm{pv}\to\mathrm{batt}}_t + E^{\mathrm{nät}\to\mathrm{batt}}_t$$
   $$E^{\mathrm{urladd},t} = E^{\mathrm{batt}\to\mathrm{hus}}_t + E^{\mathrm{batt}\to\mathrm{nät}}_t$$

---

## 4. Målfunktion (Ekonomisk minimering)

I grundversionen (steg 1) minimeras den totala energikostnaden plus batterislitage minus värdet av kvarvarande energi vid horisontens slut:

$$J = \sum_{t=0}^{191} \Bigl( p^{\mathrm{in}}_t E^{\mathrm{in}}_t - p^{\mathrm{ut}}_t E^{\mathrm{ut}}_t \Bigr) + C_{\mathrm{batteri}} - V(SOC_{\mathrm{slut}})$$

Där:
* $p^{\mathrm{in}}_t$: Faktiskt marginalpris för import (spotpris + energiskatt + överföringsavgift + moms + påslag).
* $p^{\mathrm{ut}}_t$: Faktiskt marginalvärde för export (spotpris + skattereduktion 60 öre om tillämpligt + nätnytta − avgifter).
* $C_{\mathrm{batteri}}$: Slitagekostnad för battericykling (riktvärde ca 30 öre/kWh helcykel, t.ex. 15 öre/kWh laddning och 15 öre/kWh urladdning).
* $V(SOC_{\mathrm{slut}})$: Terminalvärde för kvarvarande energi vid $t = 192$, så att optimeringen inte tömmer batteriet artificiellt i slutet av 48-timmarsfönstret.

### SOC-dynamik och verkningsgrad:
$$SOC_{t+1} = SOC_t + \eta_{\mathrm{ladd}} E_{\mathrm{ladd},t} - \frac{E_{\mathrm{urladd},t}}{\eta_{\mathrm{urladd}}}$$

---

## 5. Begränsningar (Constraints)

1. **Batterikapacitet:**
   $$0 \le SOC_t \le 20\ \text{kWh}, \quad \forall t \in [0, 192]$$
2. **Effektgränser batteri (15 kW max):**
   $$0 \le E_{\mathrm{ladd},t} \le 3{,}75\ \text{kWh}$$
   $$0 \le E_{\mathrm{urladd},t} \le 3{,}75\ \text{kWh}$$
3. **Icke-samtidig laddning och urladdning:**
   Fysiskt kan växelriktaren inte ladda och urladda batteriet samtidigt. Säkerställs via formulering eller binär variabel:
   $$z_t \in \{0, 1\}, \quad E_{\mathrm{ladd},t} \le 3{,}75 z_t, \quad E_{\mathrm{urladd},t} \le 3{,}75 (1 - z_t)$$
4. **Nätanslutningens timgräns (14 kW max timmedel):**
   För varje hel klocktimme $h$ (bestående av fyra fasta kvartar $t \in h$):
   $$\sum_{t \in h} E^{\mathrm{in}}_t \le 14\ \text{kWh}$$
   *(Obs: gäller per fast klocktimme, ej rullande fönster).*

---

## 6. Terminalvärdering ($V(SOC_{\mathrm{slut}})$)

* Utgående SOC tvingas **inte** till startvärdet $SOC_0$, för att undvika onödiga forcerade laddningar/urladdningar.
* $V(SOC_{\mathrm{slut}})$ modelleras linjärt eller styckvis linjärt som:
  $$V(SOC_{\mathrm{slut}}) = \lambda_{\mathrm{term}} \cdot SOC_{\mathrm{slut}}$$
  där $\lambda_{\mathrm{term}}$ representerar förväntat alternativvärde av lagrad el efter $T=192$ (t.ex. medelvärde av kommande dygns importpriser minus cykelkostnad).

---

## 7. Matematisk lösningsmetod

* Modellen formuleras som ett **MILP (Mixed-Integer Linear Program)** eller **LP (Linear Program)** om binära variabler kan undvikas tack vare strikt positiva marginalkostnader ($p^{\mathrm{in}}_t > p^{\mathrm{ut}}_t$).
* Lösare i Python: `PuLP`, `SciPy (HiGHS)`, eller `Pyomo` med fri solver (HiGHS / CBC).
* Beräkningstid för 192 intervall i HiGHS är typiskt $< 0{,}1$ sekunder, vilket gör 15-minuters rullande exekvering i Home Assistant mycket lättdrivet.

---

## 8. Utökning: Effektavgift (Steg 2)

I steg 2 integreras den faktiska nättaxan (135 kr/kW·månad för snittet av månadens 3 högsta timtoppar kl 07:00–20:00 vardagar nov–mar):

* Variabel för månadens toppnivåer läggs in i målfunktionen.
* Modellen kommer då automatiskt att hålla nere timimporten under 07–20 vardagar, sprida laddning över tid och reservera batterikapacitet för dagsbehovet, utan behov av separata heuristiska regler.

---

## 9. Kvar att fastställa inför implementation

1. **Verkningsgrader:** Exakta $\eta_{\mathrm{ladd}}$ och $\eta_{\mathrm{urladd}}$ för Solis + batterikombinationen (t.ex. $0{,}95 \times 0{,}95 \approx 0{,}90$ round-trip).
2. **Cykelkostnad:** Precisera fördelningen (15 öre in / 15 öre ut, eller 30 öre per urladdad kWh).
3. **Lösare:** Val av bibliotek (rekommendation: `scipy.optimize.linprog` med HiGHS-metoden eller `pulp` med HiGHS).
4. **Testplan:** Köra första test offline mot historiska data från 2025 för att verifiera beteendet mot backmetoden.
