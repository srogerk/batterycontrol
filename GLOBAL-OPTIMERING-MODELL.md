# Global optimeringsmodell för batteristyrning

*Logisk grund: Rogers specifikation "Specifikation för batterycontrol" (2026-11-10, version 2).*
*Dokumentet ersätter tidigare version (2026-10-28). Vad som utgått och varför framgår av §11.*

---

## 1. Övergripande mål

Optimera **inköp, lagring, förbrukning och försäljning** av el med avseende på kostnad.

Målet uppnås inte genom lokala heuristiker ("backmetoden", fasta tidsfönster, separata
specialregler per pristopp), utan genom att alla energiflöden och fysikaliska begränsningar
formuleras matematiskt. Den ekonomiskt optimala laddnings- och urladdningsprofilen uppstår
då som en direkt följd av optimeringen — inklusive att hålla nere timimporten under
tariffönstret, sprida laddning över tid och reservera batterikapacitet för dagsbehovet.

---

## 2. Regler och begränsningar

### 2.1 Batteriet

| # | Regel |
|---|---|
| 1 | Användbar kapacitet är **85 % av 25 kWh** = **21,25 kWh**, motsvarande SOC 15–100 %. Hälsogolv 3,75 kWh, tak 25,0 kWh. |
| 2 | Lagring har en **cykelkostnad på 0,30 kr/kWh** (0,15 kr in + 0,15 kr ut). |
| 8 | Batteriets **nominella maxeffekt är 15 kW** in och ut. För att minska förluster och stress är driftgränsen **10 kW**. |

Verkningsgrad: **η_ladd = 0,95** och **η_urladd = 0,95**, dvs rundtur 0,9025.

### 2.2 Priser

| # | Regel |
|---|---|
| 3 | **Inköpspris** = spotpris **med** moms + avgifter (ca 0,50 kr/kWh). **Säljpris** = spotpris **utan** moms + nätnytta (0,07 kr/kWh). |
| 10 | Under effekttarifftid läggs effekttariffen till elleverantörens pris för inköpt el och **ingår sedan i optimerarens indata**. |

**Ingen skattereduktion** ingår i säljpriset — specifikationen anger uttryckligen bara
spotpris utan moms + nätnytta.

### 2.3 Effektavgift

| # | Regel |
|---|---|
| 4 | Gäller **säsongsvis 1/11–31/3** och **periodvis 07:00–20:00 helgfria vardagar**. |
| 5 | Avgiften är **månatlig** och baserad på energiimporten under en fast klocktimma `T:00–T+1:00`. Den utgörs av **45,00 kr/kWh** multiplicerat med summan av de **3 högsta timförbrukningarna på olika dygn** under månaden. Vid månadens slut blir den totala kostnaden alltså **135 kr/kW** gånger medelvärdet av de tre topparna. |
| 6 | **Avtalad maxeffekt in från nätet är 14 kW**, mätt på jämn timma — dvs högst **14 kWh under en timma** (minut 00–00). |
| 7 | För marginal mot avtalet sätts driftgränsen till **13,5 kW** timmedel. |

### 2.4 Effektpåslag för att undvika på/avslag

| # | Regel |
|---|---|
| 9 | Låga laddeffekter premieras, balanserat mellan prisoptimering och antal av-/påslag. **Påslaget är på batteriets effekt**, inte på nätimporten — det exkluderar alltså husförbrukning och billaddning. En inköpsprisskillnad på **0,20 kr** är brytpunkten för att flytta 6 kW till 5+1 kW, vilket ger ett **linjärt prispåslag på 0,04 kr per kW** batterieffekt. |

### 2.5 Billaddning

| # | Regel |
|---|---|
| 11 | Billaddning kan **bokas**. Då allokeras ett block om **3 sammanhängande timmar** med ca **5,5 kW** (8 A × 3 × 240 V) billaddning/inköp till optimalt totalt inköpspris. |
| 12 | Laddblocket ligger mellan **22:00 och 07:00**. |

---

## 3. Optimeringsmodell

### 3.1 Horisont och rullande omplanering

| Parameter | Värde |
|---|---|
| **Tidsupplösning** | 15 minuter ("kvartar") |
| **Planeringshorisont** | Dynamisk — till spotprisets horisont (§3.1 nedan) |
| **Omplaneringsfrekvens** | Varje kvart; endast första kvartet verkställs |
| **Ingående SOC** | Faktiskt uppmätt värde vid planens start |
| **Utgående SOC** | Fri variabel, värderas via terminalfunktion (§3.6) |

Optimeraren beräknar de totalt mest lönsamma effekterna för **resterande 15-minutersperioder
("kvartar") fram till spotprisets horisont**. Utifrån de beräknade optimala effekterna för
**innevarande kvart** ställs växelriktaren (inklusive batteriet) in att verkställa dem.

**Spotprisets horisont:**
- **Före kl 13:** till kl 24 innevarande dygn.
- **Efter kl 13:** innevarande dygn + nästa dygn.

Horisonten är alltså **dynamisk** och avgränsas strikt till de kvartar som faktiskt har känt
spotpris. Påhittade eller upprepade priser i framtiden undviks helt.

Modellen är en **MPC** (Model Predictive Control): varje kvart löses problemet om med
uppdaterad SOC och uppdaterade prognoser, och endast det första kvartet verkställs.

### 3.2 Flödesmodell

El förbrukas **enbart genom hushållsanvändning**. I denna version säljer systemet
**inte batteriel** — den export som modelleras är enbart solel som blir över.

Fyra flöden måste hela tiden vara i balans: **från nätet, in i batteriet, ut ur batteriet,
till hushållet.**

För att garantera energibalansen och förhindra **dubbelräkning** eller **fiktiv rundgång**
modelleras flödena explicit för varje kvart.

Variabler per kvart `t`:

| Variabel | Betydelse |
|---|---|
| `E_grid_load` | Nätenergi till hushållet |
| `E_grid_batt[i]`, i = 0..4 | Nätenergi till batteriet, uppdelad i 5 effekttrappsteg |
| `E_batt_load` | Batterienergi till hushållet |
| `E_pv_load` | Solel till hushållet |
| `E_pv_batt` | Solel till batteriet |
| `E_pv_grid` | Solel till nätet (export) |
| `dP_chg` | Ändring i batteriladdning mellan kvartar (växlingsdämpning) |
| `SOC_t` | Batteriets lager vid början av kvart `t` |

**Balansekvationer:**

1. **Huslastbalans:** `E_grid_load + E_batt_load + E_pv_load = E_load[t]`
2. **Solbalans:** `E_pv_load + E_pv_batt + E_pv_grid = E_pv[t]`
3. **Nätimport:** `E_grid_load + Σ E_grid_batt[i] = E_import[t]`
4. **Batteri in/ut:** `E_ladd = Σ E_grid_batt[i] + E_pv_batt`, `E_urladd = E_batt_load`
5. **SOC-dynamik:** `SOC_{t+1} = SOC_t + η_ladd · E_ladd − E_urladd / η_urladd`

### 3.3 Kostnadskategorier

Optimeraren tar hänsyn till **alla** priskategorier. Redovisningen av vinst visar dem separat.

| Kategori | Ingående |
|---|---|
| **Faktisk** | Nätimport till spotpris + avgifter + effekttariff. Undre tariffgränsen har negativt påslag — för optimeraren är import **gratis upp till den gränsen**. |
| **Slitage** | Cykelkostnad 0,15 kr/kWh in + 0,15 kr/kWh ut. |
| **Optimering** | Påslag som styr beteendet, inte faktisk kostnad: effekttrappsteg, växlingsdämpning, av-/påslag. |

### 3.4 Målfunktion

$$J = \sum_{t} \Bigl( p^{in}_t E_{import,t} - p^{ut}_t E_{pv\_grid,t} \Bigr) + C_{slitage} + C_{optimering} - V(SOC_{slut})$$

**Optimeringspåslag (kategori "optimering"):**

- **Effekttrappsteg på batteriets ladd- och urladdningseffekt.** Fem steg om 2 kW (0–10 kW).
  Påslaget är linjärt **0,04 kr per kW** och ackumuleras per steg, så att stegmittpunkterna
  1/3/5/7/9 kW straffas med 0,04/0,12/0,20/0,28/0,36 kr. Vid 5 kW är påslaget 0,20 kr, dvs
  en annan kvart är mer fördelaktig upp till 0,20 kr prisskillnad — exakt brytpunkten i
  regel 9.
- **Växlingsdämpning.** Ett påslag på batterieffekten in och ut för **effekt i intilliggande
  kvartar**.
- **Av-/påslag.** Ett påslag när batteriladdningen växlar från av till på eller på till av.

### 3.5 Begränsningar

1. **Batterikapacitet:** `3,75 kWh ≤ SOC_t ≤ 25,0 kWh` för alla `t`.
2. **Batterieffekt:** `0 ≤ E_ladd,t ≤ 2,5 kWh` och `0 ≤ E_urladd,t ≤ 2,5 kWh`
   (10 kW × 0,25 h).
3. **Icke-samtidig laddning och urladdning.** Fysiskt kan växelriktaren inte göra båda
   samtidigt. Det behöver **inte** modelleras med binär variabel: eftersom båda riktningarna
   har strikt positiva marginalkostnader (cykel + trappsteg + växlingsdämpning) är samtidig
   laddning och urladdning alltid olönsamt och utesluts av optimeringen själv. Modellen
   förblir därmed ett rent LP.
4. **Timmedeleffekt nät:** `Σ_{t ∈ h} E_import,t ≤ 13,5 kWh` för varje fast klocktimme `h`.
5. **Effekttariff:** se §4.

### 3.6 Terminalvärdering

Utgående SOC tvingas **inte** till startvärdet — det skulle framtvinga onödiga laddningar
och urladdningar. I stället värderas kvarvarande energi vid horisontens slut:

$$V(SOC_{slut}) = \lambda_{term} \cdot SOC_{slut}$$

`λ_term` sätts till **det sista kända importpriset** i horisonten, inte till ett dygnsmedel.
Det förhindrar att optimeraren sparar batterienergi mot en dyr dagel eller panikladdar till
100 % före midnatt. Vid laddning värderas energin `λ_term · η_ladd`, vid urladdning
`λ_term / η_urladd`.

### 3.7 Lösningsmetod

Modellen formuleras som ett **LP (Linear Program)** — inte ett **MILP (Mixed-Integer Linear
Program)** — eftersom de binära variablerna kan undvikas tack vare strikt positiva
marginalkostnader (§3.5 p.3). Det gör problemet både snabbare och garanterat globalt optimalt.

Lösare: **HiGHS** via `scipy.optimize.linprog` (PuLP och Pyomo med HiGHS/CBC är likvärdiga
alternativ men inte valda). Beräkningstiden för en horisont på ett dygn är typiskt under
0,1 s, vilket gör rullande 15-minutersexekvering lättdriven.

---

## 4. Effekttariff — beredande beräkning

### 4.1 Princip

En **beredande beräkning** räknar fram den effektgräns som uppstår baserat på
effekttariffens utformning, och definierar prisfunktioner för den merkostnad tariffen
genererar. Därefter utförs optimeringen som vanligt — det är skälet till att inga separata
heuristiska tariffregler behövs.

### 4.2 Prisfunktionen

Effektgränsen (taket) implementeras som en **brant prisökning från 0 vid tak-effekten**:

$$pris(P) = \max\bigl(0,\ (P - P_{tak}) \cdot 45\bigr)$$

Exempel: tak vid 2,5 kW ger `pris(P) = max(0, (P − 2,5) · 45)`.

Priset är alltså **noll ända upp till taket** och växer sedan linjärt med 45 kr per kW
överstigande timmedeleffekt. **Optimering sker på prisfunktionen för kvarts-effekt-gränsen**
(§4.5), inte på timgränsen direkt.

### 4.3 Definitioner

- Effektförbrukningen avser **alltid effekten in från nätet** och **kompenseras inte** av
  eventuell export mot nätet.
- Effektförbrukningen mäts som **medelvärdet av effekten under en klocktimma**, dvs
  energiimporten från nätet under en klocktimma.
- Tariffen är **0** om energiimporten under en klocktimma understiger det högsta av
  - den **3:e högsta** klocktimimporten under innevarande månad (MTD), och
  - den **högsta** klocktimimporten under innevarande tariffönster (07:00–20:00).
- För energi **över** den gränsen är taxan **45 kr/kWh**. Vid månadens slut räknas den
  totala kostnaden som medelvärdet av de 3 högsta topparna på åtskilda dygn × **135 kr/kWh**.
- Beräkningen handlar alltså om att hitta **brytpunkten mellan 0 och 45 kr/kWh**.
  Kostnaden tillhör kategorin **"faktisk"**.

### 4.4 Taket

Beredningen räknar fram effektgränsen:

$$P_{tak} = \max\bigl(P_{mån\text{-}min},\ P_3,\ P_{max}\bigr)$$

| Term | Betydelse |
|---|---|
| `P_mån-min` | **Undre gräns** för tariffens brytpunkt. 1,3 kW för nov, feb, mars; 1,6 kW för dec, jan. |
| `P_3` | Månadens **3:e största** timmedeleffekt — faktisk förbrukning. |
| `P_max` | **Hittills högsta faktiska** medeleffekt under en klocktimma i **innevarande tariffönster**. |

**Om `P_mån-min`:** gränsen finns för att **förhindra onödiga battericykler** orsakade av
optimering mot effektgränser som ligger under den förväntade effekttoppen för månaden. Den
avspeglar den lägst möjliga energiförbrukningen per klocktimma baserat på månadens högsta
förväntade importförbrukning under ett tariffönster. Exempel: en decemberdag där det behövs
46 kWh import, med 20 kWh tillgängligt i batteriet kl 07:00, kräver totalt 26 kWh import =
**2 kWh/h** i tariffönstret. Denna kostnad är "faktisk" och har ett **negativt prispåslag** —
för optimeraren "är det gratis upp till undre gränsen".

### 4.5 Kvartsgränser inom innevarande timme

- **Samma timgräns gäller för alla timmar framåt i planen** inom ett och samma effektfönster.
- Timgränsen kan **ändras uppåt** vid nästa kvart, när planen räknas om med delvis nya
  ingångsvärden — specifikt "högsta faktiska timmedeleffekt" i innevarande effektfönster.
- **Kvartsgränserna varierar mellan kvartarna i innevarande timme**, beroende på det
  **verkliga effektmedelvärdet upp till "nu"** och det prognostiserade medeleffektbehovet
  under resten av timmen.
- **Kvartsgränsen för framtida kvartar efter innevarande timme är samma som timgränsen.**

Innan optimeringen börjar läser beredningen av **faktisk upplupen energi-nät-import denna
klocktimma** och för in värdet i beräkningen av kvarts-prisfunktionerna. Effektgränsen för
import **sänks eller höjs** för resterande kvartar i innevarande timme så att medeleffekten
för dem hamnar **just under** nätimportens effekttak. Beräkningen består av både faktiskt
uppmätt medeleffekt och uppskattning för innevarande och framtida kvartar.

### 4.6 Indata som krävs

| Symbol | Betydelse |
|---|---|
| `P1, P2, P3` | Dygnets ingångsvärden för alla 3 månadstoppar MTD. **Orsaken:** P1, P2, P3 för morgondagens effektfönster ändras om det blir en effekttimma i dagens fönster som är högre än P3, och kanske även P2 och P1. |
| `P_now` | Hittills faktiskt importerad **medeleffekt** under innevarande timma. |
| `P_mån-min` | Månadens undre gräns för tariffens brytpunkt — 1,3 eller 1,6 kW. |
| `SOC` | Ingående SOC (tillgänglig energi i batteriet), för att uppskatta kvarvarande energi-import-behov i tariffönstret. |

Beredningen behöver beräkna:
- **Kvarvarande energi-import-behov denna timma.**
- **Kvarvarande energi-import-behov i tariffönstret.**

### 4.7 "Rädda timmen"

Kvartsgränserna i §4.5 är den mekanism som gör "rädda timmen" möjlig. Utan kompensation för
upplupen timenergi ser optimeraren bara de kvartar som återstår och tillåter full effekt som
om timmen började nu. Vid `:45` skulle det ge 13,5 kW i ett kvart = 4,97 kW timmedel mot ett
tak på 1,3 kW — en omöjlig kompensation som optimeraren inte känner till.

**Mekanism:** de kvarvarande kvartarna i innevarande klocktimme får dela på det som
**återstår** av timmens energibudget:

```
budget = P_tak · 1,0 h − E_now
```

Bindningen läggs på summan av nätimport (huslast + batteriladdning) över innevarande
klocktimmens kvarvarande kvartar. Vid negativ budget blir taket 0 — timmen är då redan
förbrukad och kan inte räddas, men optimeringen ska ändå inte föreslå absurd import.

**Detta är ett uttryckligt krav, inte en optimering:** "rädda timmen" måste fungera under
effekttaxeperioder.

---

## 5. Solpaneler

- Det finns solpaneler i invertersystemet.
- Energi från solpanelerna kan styras till:
  - **egenförbrukning**,
  - **export** av överskottet till nätet,
  - **laddning av batteriet**,
  - en **kombination** därav, eller
  - **enbart laddning av batteriet**, varvid hushållet tar sin energi från nätet.

Det sista fallet är ekonomiskt motiverat när solelen är värd mer lagrad än direkt förbrukad —
t.ex. när den kan säljas dyrare senare, eller när nätimporten under tariffönstret ändå är
gratis upp till `P_mån-min` (§4.4). Det kräver att `E_pv_batt` och `E_grid_load` kan vara
positiva samtidigt, vilket flödesmodellen i §3.2 medger.

---

## 6. Billaddning

- Billaddblocket är **fast förankrat i tiden** och ändras inte för varje kvartsoptimering.
- Optimeringen måste ta hänsyn till **var i blocket** laddningen börjar — den ska inte
  skapa ett nytt block varje kvart.
- Blocket är 3 sammanhängande timmar à ca 5,5 kW mellan 22:00 och 07:00, placerat på
  optimalt totalt inköpspris.
- Blocket är ren nätimport (ingen hänsyn till PV) och räknas in i timgränsen för
  nätimport, eftersom det konkurrerar om samma 13,5 kW.

---

## 7. Implementationsbeaktanden

1. Systemet (BatteryControl) är **integrerat med Home Assistant**.
2. Den faktiska styrningen görs via integrationen **"Solis Modbus"**.
3. Systemet har tillgång till **alla sensorer** som finns i Home Assistant.
4. Det är viktigt att **"rädda timmen"** under effekttaxeperioder implementeras och fungerar.

---

## 8. Kvar att fastställa

1. **Testplan:** köra modellen offline mot historiska data från 2025 för att verifiera
   beteendet mot "backmetoden".

*(Verkningsgrader, cykelkostnad, påslagsnivå och lösare är avgjorda: 0,95/0,95,
0,15+0,15 kr/kWh, 0,04 kr/kW, HiGHS.)*

---

## 9. Frågor från föregående dokumentversion — nu besvarade

Sex punkter var oklara i specifikationens första version. Alla är utredda i version 2 och
ligger nu inarbetade i texten ovan.

| # | Fråga | Svar |
|---|---|---|
| 1 | Regel 9 säger "0,04 **öre** per kW" men exemplet ger 0,20 kr vid 5 kW | **0,04 kr/kW.** Specens avsnitt 2 säger uttryckligen "0,04 **kr** per kW" två gånger, och 0,20 kr vid 5 kW bekräftar. §2.4. |
| 2 | Formeln för effektgränsen var avklippt | **`P_tak = max(P_mån-min, P3, P_max)`** — §4.4. |
| 3 | En mening om "timma, dygn-fönster och månad" var halv | **Utgår.** Ersatt av den utförliga beskrivningen i §4.5 om hur kvartsgränser varierar inom timmen men är konstanta framåt. |
| 4 | "**Negativt** optimeringspåslag för effekt i intilliggande kvartar" — koden har positivt | **Positivt påslag.** Version 2 säger "ett optimerings-påslag på batterieffekten in och ut för effekt i intilliggande kvartar" — ordet "negativt" är borta. §3.4. |
| 5 | "Säljer systemet inte el" samtidigt som koden intäktsför PV-export | **"säljer systemet inte batteri-el"** — förtydligat i version 2. PV-export är avsedd och modellerad. §3.2. |
| 6 | Skattereduktion 60 öre i säljpriset? | **Nej.** Säljpris = spot utan moms + nätnytta 0,07. §2.2. |

**Nytt i version 2 som inte fanns i version 1:**

- **Prisfunktionen som ramp** — `pris(P) = max(0, (P − P_tak) · 45)` (§4.2). Tidigare version
  beskrev bara att en "prisfunktion" skulle definieras, inte hur.
- **`P_max`** — hittills högsta faktiska timmedeleffekt i innevarande tariffönster, som
  tredje term i taket (§4.4).
- **Kvartsgränser vs timgräns** — att timgränsen är konstant framåt i fönstret medan
  kvartsgränserna varierar inom innevarande timme (§4.5). Se §10 p.6: detta är bara
  delvis byggt i koden.
- **Eget avsnitt om solpanelerna** (§5), inklusive fallet att hushållet tar energi från
  nätet medan solelen enbart laddar batteriet.

---

## 10. Gap mellan specifikationen och koden

Punkter där specifikationen kräver något som `src/` ännu inte har. **Detta är
implementationsuppgifter, inte oklarheter i specifikationen.**

1. **`P_max` saknas.** Specen kräver "hittills högsta faktiska medeleffekt under en
   klocktimma i innevarande tariffönster". Ingen sådan storlek beräknas i koden
   (0 grep-träffar på `Pmax`/`Emax`).

2. **`P_3` är hårdkodad.** Koden använder `p3_hist = 2.49` från ett engångsbesked från
   Göteborg Energi. Specen kräver `P1, P2, P3` **MTD**, uppdaterade — eftersom dagens
   effekttimma kan tränga in i topp-3 och ändra morgondagens ingångsvärden.

3. **`p_eft_prognos` finns i koden men inte i specen.** Kodens tröskel är
   `max(p_tak, P3, p_eft_prognos)`. Specens formel är `max(P_mån-min, P3, P_max)` — prognos-
   termen är alltså **borttagen** och ersatt av `P_max`. Termen behöver antingen tas bort
   eller motiveras.

4. **Taket är per dag i koden, per effektfönster i specen.** Koden lägger en
   `P_peak_excess`-variabel och ett `p_thresh` **per tariffdag**. Specen säger att *samma*
   timgräns gäller för alla timmar framåt inom ett effektfönster. Vid horisonter som spänner
   över dygnsgränsen behöver detta stämmas av.

5. **Prisfunktionen är en slack-variabel i koden, en ramp i specen.** Matematiskt ger de
   samma marginalkostnad (45 kr per kW överstigande timmedel), men formuleringarna skiljer
   sig. Värt att verifiera att de ger identiska planer.

6. **Kvartsgränserna i §4.5 är bara delvis byggda.** Koden har "rädda timmen" för
   innevarande timme (`rescue_hour`), men ingen generell kvartsgräns som varierar mellan
   kvartarna i timmen enligt formeln i §4.5 — och ingen som sänker/höjer per kvart.

---

## 11. Information från tidigare version — status

Varje uppgift i dokumentet från 2026-10-28, med bedömning. Poster som utgår anges med skäl.

| Uppgift i tidigare version | Status |
|---|---|
| **MPC-princip, rullande omplanering varje kvart** | **Kvar.** Formuleringen i §3.1. |
| **"Ersätter lokala heuristiker (backmetoden, fasta tidsfönster)"** | **Kvar** som grundfilosofi, §1. |
| **Horisont 48 timmar, T = 192** | **Utgår.** Ersätts av dynamisk horisont till spotprisets slut (§3.1). En fast 48-timmarshorisont kräver påhittade priser bortom känd data. |
| **Användbar kapacitet 20 kWh** | **Utgår — fel.** 85 % av 25 kWh = 21,25 kWh (3,75–25,0). |
| **Maximal effekt 15 kW** | **Utgår.** 15 kW är HW-nominellt (regel 8); driftgräns 10 kW. |
| **Maximal energi per kvart 3,75 kWh** | **Utgår.** 10 kW × 0,25 h = 2,5 kWh. |
| **Maximal nätimport 14 kW timmedel** | **Kvar** som avtalsgräns (regel 6), med driftgräns 13,5 kW (regel 7). |
| **Solproduktion: topp ca 8 kW, upp till ~70 kWh/dag** | **Kvar.** Prognos per kvart, §3.2. |
| **Husförbrukning: prognos per kvart** | **Kvar**, §3.2. |
| **Prisdata: spotpris per kvart + marginalpriser** | **Kvar**, förtydligat som separata `p_in` / `p_out` (§2.2, §3.4). |
| **Ingående SOC = faktiskt uppmätt vid planens start** | **Kvar**, §3.1. |
| **Utgående SOC fri variabel med terminalfunktion** | **Kvar**, §3.6 — med exakt implementation (sista kända importpriset). |
| **Åtta flödesvariabler inkl. `E_batt→nät`** | **Delvis utgår.** PV-export finns; batteriexport är inte implementerad och enligt §3.2 inte avsedd i denna version. |
| **Curtailment (`E_curtail`)** | **Utgår tills vidare.** Solens balans är en likhet — all sol måste användas, lastas eller exporteras. Får återinföras om soleffekten kan överstiga exportkapaciteten. |
| **`p_ut` = spot + skattereduktion 60 öre + nätnytta − avgifter** | **Utgår.** Nya specen anger uttryckligen spot utan moms + nätnytta 0,07 — ingen skattereduktion. |
| **`C_batteri` ca 30 öre/kWh helcykel** | **Kvar**, preciserat 0,15 + 0,15 (§2.1). |
| **SOC-dynamik med verkningsgrad** | **Kvar**, §3.2. η = 0,95/0,95. |
| **Binär variabel `z_t` för icke-samtidighet** | **Utgår.** Bevisas onödig av strikt positiva marginalkostnader (§3.5 p.3) — modellen förblir LP. |
| **Timgräns 14 kWh per klocktimme** | **Kvar**, ändrad till 13,5 (§3.5 p.4). |
| **MILP/LP, PuLP / SciPy HiGHS / Pyomo** | **Kvar**, valet är HiGHS via `scipy.optimize.linprog` (§3.7). |
| **Beräkningstid < 0,1 s** | **Kvar** som not, §3.7. |
| **Effektavgift 135 kr/kW·månad för 3 högsta topparna** | **Kvar** — 45 kr/kWh × 3 toppar = 135 kr/kW (§2.3 regel 5). |
| **"Utan behov av separata heuristiska regler"** | **Kvar** som princip, §1 och §4.1. |
| **Kvar att fastställa: verkningsgrader** | **Avgjort** — 0,95/0,95. |
| **Kvar att fastställa: cykelkostnadens fördelning** | **Avgjort** — 0,15 in / 0,15 ut. |
| **Kvar att fastställa: val av lösare** | **Avgjort** — HiGHS. |
| **Kvar att fastställa: testplan mot historiska 2025-data** | **Kvar**, §8. |
