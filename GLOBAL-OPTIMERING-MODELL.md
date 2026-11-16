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

**Elbilslasten är inte en variabel utan en konstant.** När en laddorder finns bokas ett fast
3-timmarsblock (`src/ev_block.py`) och dess effekt läggs in som **känd nätimport** i de
kvartar blocket täcker. Den syns därför i högerledet på timmedeleffektsraderna (§4.5) men
aldrig som ett optimeringsval — bilen ska ladda 10 kWh oavsett spotpris, det är bara
*fönstret* som valdes när ordern lades.

**En slackvariabel per tariffdag** (`P_peak_excess`) ligger utanför kvartsvariablerna. Den
representerar dagens högsta överskridande av `P_tak` och straffas med 45 kr/kW i målfunktionen
— det är den LP-form som gör `max(0, P − P_tak) · 45` linjär. Det måste vara **en per dag**,
inte en per timme: tariffen räknar bara den högsta timmen per dygn, så en variabel per timme
skulle straffa samma överskridande upp till 13 gånger.

**En slackvariabel per klocktimme** (`S_h`, införd 2026-11-17) ligger också utanför
kvartsvariablerna, för 13,5 kW-raden i §4.5. Där är *en per timme* rätt, tvärtom mot
tariffraden: 13,5 kW är ett tak **per timme**, inte per dygn, så varje timme har sitt eget
överskridande. Pris 90 kr/kW.

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
4. **Timmedeleffekt nät:** `Σ_{t ∈ h} E_import,t + E_nu − S_h ≤ 13,5 kWh` för varje fast
   klocktimme `h`, där `E_nu` är energi redan importerad i timmen och `S_h ≥ 0` är en
   slackvariabel prissatt till **90 kr/kW** (§4.5). En klocktimme är alltid
   1,0 h — divisorn är aldrig antalet kvartar som råkar återstå i horisonten.

   **Raden är inte hård.** Fram till 2026-11-17 var den det, med `max(0, …)`-klamning i
   högerledet, och modellen blev `Infeasible` så fort lasten översteg 13,5 kW en hel timme
   (15 kW och 20 kW testades, oavsett SOC). Men taket är inte fysiskt: koden skriver aldrig
   någon nätimportgräns till växelriktaren (`peak_max_usable_grid_power` lämnas orörd), och
   huset får alltid sin effekt. Ett hårt tak vore en lögn om verkligheten.

   90 kr/kW, dubbelt tariffpriset, för att ett fjärde överskridande under avtalsperioden
   enligt Göteborg Energis villkor hissar abonnemanget till 43 kW. Fysisk backstop är
   huvudsäkringen: 20 A × 3 × 230 V = 13,8 kW.
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

### 3.8 Verkställighet — från plan till växelriktare

Modellen producerar en plan med **både laddning och urladdning** som variabler. Växelriktaren
har dock **bara en skrivbar kanal för laddning** och ingen alls för urladdning. Det glappet
måste beskrivas, annars ser modellen ut att styra något den inte styr.

| Planens storhet | Verkställs genom | Karaktär |
|---|---|---|
| **Nätladdning** `Σ E_grid_batt[i]` | `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1` (TOU Slot 1) | Direkt. Effekten räknas om till ampere via batterispänningen och fördelas jämnt över kvartet. |
| **Urladdning** `E_batt_load` | `number.solis_s6_eh1p_backup_soc` | **Indirekt.** Ingen urladdningskanal finns. Spärren sätts till aktuell SOC för att stänga av Self-Use, eller till 15 % för att släppa den fri. |
| **Elbilsladdning** | `chargeamps.enable` / `disable` + `set_max_current` | Direkt, fast 8 A. |

**Urladdning är alltså inte styrd, den är tillåten eller förbjuden.** Växelriktaren kör
Self-Use och parerar huslasten själv ner till `backup_soc`-golvet. Modellens urladdningsvariabel
blir därför ett *binärt* beslut i praktiken — "släpp Self-Use fri eller inte" — medan LP:n
räknar på den som en kontinuerlig mängd. Det är en medveten förenkling: Self-Use parerar
transienter som modellen inte ser (spis, kaffebryggare), vilket är önskvärt under tariffen
(§4.8) men gör att den faktiska urladdningen kan avvika från planen.

**Konsekvens för modellens giltighet:** laddning är exakt styrbar, urladdning är en
övre gräns. Planens urladdningssiffror ska läsas som "så mycket får batteriet bidra", inte
"så mycket kommer det att bidra".

---

### 3.9 Prognosmodellen — optimerarens indata

LP:n är bara så bra som `E_load[t]` och `E_pv[t]`. Prognosen är därför en del av modellen,
inte ett försteg utanför den.

**Huslast** byggs i två lager:

1. **Parametrisk baslast.** Värmebehov som en linjär funktion av utetemperaturen under ett
   tröskelvärde (ca 75 W per grad under 17 °C — värmepumpen), plus en tidsprofilsberoende
   hushållsel med morgon- och kvällstopp och helgvariation.
2. **Adaptiv bias (EWMA).** Avvikelsen mellan föregående prediktion och **momentant uppmätt
   huslast** integreras med inlärningshastighet 0,20 och **begränsas till ±1,5 kW**:

$$b_{k+1} = \mathrm{clip}_{\pm 1{,}5}\bigl((1-\alpha)\,b_k + \alpha\,(P_{mätt} - P_{pred})\bigr), \quad \alpha = 0{,}20$$

Biasen fångar det som den parametriska modellen inte vet: att huset är varmare än
prognosen, att någon lagar mat, att en extra värmepump går. **Begränsningen till ±1,5 kW är
viktig** — utan den kan en enda felaktig mätning förskjuta hela prognosen i timmar.

Uppmätt huslast är **netto efter elbilen**: `sensor.ev_power` (kW) subtraheras, annars
skulle billaddningen läras in som permanent huslast och prognosen aldrig återhämta sig.

**Solprognos** kommer från Solcast (48 h, kvartsupplöst), **temperatur** från SMHI:s
timprognos interpolerad till kvartar, och **utegivaren NIBE BT1** används för den momentana
avvikelsen.

**Kvart 0 i prognosen är den enda som är mätt.** Resten är modell. Det är därför
återkopplingen i §4.5 (E_nu) och risk­taket i §4.8 behövs: de gör planen motståndskraftig
mot att prognosen har fel, i stället för att kräva att den har rätt.

---

## 4. Effekttariff — som prisfunktion

### 4.1 Princip

Effekttariffen är **inte en regel som ligger ovanpå optimeringen**. Den är en
**prisfunktion** som säger vad en extra kilowattimme i en klocktimme kostar. När
prisfunktionen är korrekt behövs inga separata tariffheuristiker: optimeraren väljer
automatiskt att hålla sig under taket när det är billigare än att betala påslaget, och att
gå över det när det är billigare än alternativet.

Det som behövs är därför två saker, och inget mer:

1. **Ett tak** `P_tak` — brytpunkten där tariffen slår till (§4.4).
2. **En prisfunktion** som är noll upp till taket och 45 kr/kW över det (§4.2).

Allt övrigt i detta kapitel är definitioner som behövs för att räkna fram `P_tak` korrekt.

### 4.2 Prisfunktionen

Kostnaden för en klocktimme med timmedeleffekten `P`:

$$\text{kostnad}(P) = \max\bigl(0,\ (P - P_{tak}) \cdot 45\bigr) \quad [\text{kr}]$$

Priset är alltså **noll ända upp till taket** och växer sedan linjärt med 45 kr per kW
överstigande timmedeleffekt. Exempel: tak 2,5 kW ger `kostnad(P) = max(0, (P − 2,5) · 45)`.

Två enhetsfakta som måste hållas isär:

- **45 kr/kW gäller timmedeleffekt**, inte kvartseffekt. Att höja *en* kvart med 1 kW höjer
  timmedelvärdet med 0,25 kW och kostar därmed **11,25 kr** — inte 45 kr.
- **Spottermen skalas med 0,25 h.** En kvarts energi är `P · 0,25` kWh, så spotkostnaden för
  en kvart är `pris · P · 0,25`. Att utelämna 0,25:an gör spottermen fyra gånger för dyr och
  rubbar hela avvägningen mot tariffen.

### 4.3 Definitioner

- Effektförbrukningen avser **alltid effekten in från nätet** och **kompenseras inte** av
  eventuell export mot nätet.
- Effektförbrukningen mäts som **medelvärdet av effekten under en klocktimma** (minut 00–00),
  dvs energiimporten från nätet under en klocktimma delat med **1,0 h**.
- Tariffen är **0** om timmedeleffekten understiger `P_tak` (§4.4).
- För effekt **över** `P_tak` är taxan **45 kr/kW** (per timmedeleffekt, alltså samma tal som
  45 kr/kWh eftersom tidsenheten är 1,0 h). Vid månadens slut räknas den totala
  kostnaden som medelvärdet av de 3 högsta topparna på åtskilda dygn × **135 kr/kW**.
- Beräkningen handlar alltså om att hitta **brytpunkten mellan 0 och 45 kr/kW**.
  Kostnaden tillhör kategorin **"faktisk"**.

### 4.4 Taket

$$P_{tak} = \max\bigl(P_{mån\text{-}min},\ P_3,\ P_{max},\ P_{eft\_prognos} \cdot (1+m)\bigr)$$

| Term | Betydelse |
|---|---|
| `P_mån-min` | **Undre gräns** för tariffens brytpunkt. 1,3 kW för nov, feb, mars; 1,6 kW för dec, jan. |
| `P_3` | Månadens **3:e största** timmedeleffekt — faktisk förbrukning. |
| `P_max` | **Hittills högsta faktiska** medeleffekt under en klocktimma i **innevarande tariffönster**. |
| `P_eft_prognos` | Prognostiserat **kvarvarande importbehov** i tariffönstret, utslaget per timme — ett *golv*, inte ett tak. Marginalen `m` (10 %) läggs här, uppströms, inte i `max()` (§4.4 nedan). |

**Varför `P_max` ingår:** en redan tagen topp är **sjunket pris**. Har timmen kl 10 redan
gått över taket kostar det ingenting extra att göra det igen — marginalkostnaden för nästa
kilowatt är noll upp till den redan uppnådda nivån. Att *inte* ha med `P_max` skulle få
optimeraren att kämpa mot ett tak den redan spräckt, vilket ger onödiga cykler och utebliven
vinst.

**Varför `P_3` och inte enbart `P_eft_prognos`:** prognostiserat kvarvarande importbehov kan
inte avgöra om en topp *blir* en av månadens tre högsta — det beror på vad som händer resten
av månaden. Att använda prognosen som *enda* tröskel gör taket godtyckligt högt på svaga
dagar och låser batteriet. `P_3` är ett faktiskt, känt värde.

**Men `P_eft_prognos` ingår sedan 2026-11-17 i `max()` tillsammans med de tre andra.** Den
togs bort 2026-11-13 med ovanstående motivering, men argumentet gäller bara när prognosen är
*enda* tröskeln. Inne i en `max()` kan den bara **höja** taket, aldrig sänka det — så på
svaga dagar ger den ett lågt värde och påverkar ingenting. Kvar står den mekanism den
skyddar mot: att dygnets energibehov helt enkelt **inte ryms** under tröskeln, vilket är
precis vad som gjorde LP:n `Infeasible` vid hög last.

Marginalen läggs **uppströms på prognosen** (`P_EFT_MARGIN_PCT`, 10 %), inte i `max()`-termen:
`p_eft_prognos` är ett *golv* — minsta genomsnittliga import som krävs — och att sätta
tröskeln under det skapar ingen marginal, bara en garanterad straffavgift. Marginalen ska
göra att ett något högre *faktiskt* behov fortfarande ryms under taket, och då hör den i hur
prognosen räknas fram, inte i en konstgjord justering av ett ärligt ingångsvärde.

Marginalens storlek är ännu inte kalibrerad — den är satt till 10 % som en utgångspunkt, och
`[TROSKEL]`-loggen skriver nu `p_tak`, `P3`, `P_max`, `p_eft` och vilken term som driver
tröskeln, så att den kan bytas mot ett mätt värde efter några dagars drift.

**Om `P_mån-min`:** gränsen finns för att **förhindra onödiga battericykler** orsakade av
optimering mot effektgränser som ligger under den förväntade effekttoppen för månaden. Den
avspeglar den lägst möjliga energiförbrukningen per klocktimma baserat på månadens högsta
förväntade importförbrukning under ett tariffönster. Exempel: en decemberdag där det behövs
46 kWh import, med 20 kWh tillgängligt i batteriet kl 07:00, kräver totalt 26 kWh import =
**2 kWh/h** i tariffönstret. Denna kostnad är "faktisk" och har ett **negativt prispåslag** —
för optimeraren "är det gratis upp till undre gränsen".

### 4.5 En prisfunktion per klocktimme — inklusive den pågående

Prisfunktionen läggs på **varje klocktimme i horisonten**, med timmen som enhet. Det ger en
rad per timme:

$$\sum_{q \in h} E_q \; + \; E_{nu} \; + \; E_{elbil} \;\le\; P_{tak} \cdot 1{,}0\ \text{h}$$

där `E_q` är de **optimerade** nätimportvariablerna i kvart `q` (huslast + batteriladdning),
`E_nu` är den energi som **redan** importerats i timmen och `E_elbil` är den bokade
elbilslasten — en känd konstant, inte ett optimeringsval (§3.2).

**Divisorn är alltid 1,0 h.** En klocktimme är en timme, oavsett hur många av dess kvartar
som råkar finnas kvar i horisonten. När klockan är 14:45 innehåller horisonten bara **en**
kvart av timme 14 — men tariffen mäts över alla fyra. Räknar man med de kvarvarande kvartarnas
längd som divisor (0,25 h) tror modellen att timmen började nu och tillåter fyra gånger för
hög effekt.

**`E_nu` är det som gör "rädda timmen" onödigt som separat mekanism.** Utan `E_nu` i
högerledet saknar prisfunktionen sin egen indata: den vet hur mycket timmen *får* kosta men
inte hur mycket den *redan* kostat. Med `E_nu` med i formeln är en pågående timme exakt
lika korrekt modellerad som en framtida, och ingen heuristik behövs (§4.7).

På LP-form blir raden, med `P_peak_excess` (§3.4) som slackvariabel för dagens
överskridande:

$$\sum_{q \in h} E_q \;-\; 1{,}0 \cdot P_{peak\_excess} \;\le\; P_{tak} \;-\; E_{nu} \;-\; E_{elbil}$$

- **Framtida timmar** har `E_nu = 0` och raden är den rena takbegränsningen.
- **Innevarande timme** får ett högerled som krymper i takt med att timmen förbrukas. Är
  `E_nu` redan över taket blir högerledet negativt, **och slackvariabeln absorberar
  överskottet till 45 kr/kW**. Raden är därför alltid genomförbar — den prissätter
  överträdelsen i stället för att förbjuda den. (Före 2026-11-17 fanns en separat rad utan
  slack som förbjöd den; se §4.7.)
- **Timgränsen kan ändras uppåt vid nästa kvart**, när planen räknas om med delvis nya
  ingångsvärden — specifikt `P_max` i innevarande effektfönster.

### 4.6 Indata som krävs

| Symbol | Betydelse |
|---|---|
| `P1, P2, P3` | Dygnets ingångsvärden för alla 3 månadstoppar MTD. **Orsaken:** P1, P2, P3 för morgondagens effektfönster ändras om det blir en effekttimma i dagens fönster som är högre än P3, och kanske även P2 och P1. |
| `P_now` | Hittills faktiskt importerad **medeleffekt** under innevarande timma. |
| `E_nu` | Hittills faktiskt importerad **energi** (kWh) under innevarande timma — den direkta indatan till §4.5. |
| `P_mån-min` | Månadens undre gräns för tariffens brytpunkt — 1,3 eller 1,6 kW. |
| `SOC` | Ingående SOC (tillgänglig energi i batteriet), för att uppskatta kvarvarande energi-import-behov i tariffönstret. |

### 4.7 "Rädda timmen" — borttagen 2026-11-17

**Tidigare stod det här att timmen under effekttaxeperioder *ska* räddas, att det inte är en
avvägning optimeraren får göra mot 45 kr/kW, och att en extra rad utan slack därför lades på
innevarande klocktimme.** Den raden finns inte längre. Det här avsnittet beskriver varför,
eftersom motsatsen var uttryckligen specificerad.

**Vad raden gjorde.** Den lade ett hårt tak på innevarande klocktimme:

$$\sum_{q \in h_{nu}} E_q \;\le\; \max\bigl(0,\ P_{tak} \cdot 1{,}0\ \text{h} \;-\; E_{nu} \;-\; E_{elbil}\bigr)$$

**Varför den inte fungerade.** `max(0, …)`-klamningen gjorde att när timmens budget väl var
förbrukad blev högerledet noll, vilket tvingade *all* återstående import i timmen till exakt
noll. Med ett tomt batteri är det omöjligt — huset behöver sin last ändå. LP:n blev då
`Infeasible` och **hela planen föll**, inte bara den kvarten.

Det hände tre gånger 2026-11-12 (16:30, 17:00, 18:00, HiGHS Status 8), alla med SOC på eller
under golvet 3,75 kWh.

**Varför den inte behövs.** §4.5 hanterar redan innevarande timme korrekt via `E_nu` i
högerledet. Rad 7 har dessutom slackvariabeln `P_peak_excess`, så en överträdelse kostar
45 kr/kW i stället för att göra modellen omöjlig — det är samma prissättning som för alla
andra timmar, och det är rätt: en överträdelse som redan skett kan inte ogöras av att
modellen vägrar planera.

**Det avgörande argumentet är dock ett annat, och det är fysiskt.** Vårt eget
nätimporttak i växelriktaren är **inte hårt**. Huset får alltid sin effekt — växelriktaren
prioriterar self-use och tar resten från nätet. Ett hårt tak i LP:n är därför en lögn om
verkligheten, och `Infeasible` betyder aldrig "omöjligt i huset", bara "omöjligt i
modellen". En modell som säger nej till en situation som huset hanterar utan problem är
sämre än en modell som prissätter den.

**Vad som gäller i stället:** ingen separat räddningsmekanism. Innevarande timme modelleras
av rad 7 (tariff, 45 kr/kW slack) och rad 6 (13,5 kW, 90 kr/kW slack, §4.5). Båda är
prissättningar, inte garantier.

**Följd för §11 rad 4 och 5:** de säger att `rescue_hour` är kvar som skyddsnät utan slack.
Det stämmer inte längre — raden är borta, och `hour_threshold_kw` i `optimizer_v2.py` är en
oanvänd parameter som behålls bara för bakåtkompatibilitet.

### 4.8 Risktak per kvart — robusthet, inte tariff

Vid sidan av tariffprisfunktionen finns ett **hårt kvartstak** på nätimporten:

$$E_q \;\le\; P_{risk} \cdot 0{,}25\ \text{h}$$

Detta är **inte** en del av effekttariffen och **inte** en ekonomisk avvägning. Det är ett
robusthetstak, och skälet är att tariffen mäts som *medelvärde*: att lägga hela timmens
energibudget i det billigaste kvartet är **ekonomiskt optimalt** under en ren prisfunktion.
Timmedelvärdet blir detsamma oavsett hur importen fördelas inom timmen, så prisfunktionen är
likgiltig — och då väljer LP:n gärna ett enda kvart.

Det beteendet är oönskat av fyra skäl som prisfunktionen inte kan uttrycka:

| Skäl | Varför prisfunktionen inte räcker |
|---|---|
| **Prognosfel** | Kvartens fördelning bygger på prognoser. Ett enda kvart med 6 kW kräver att prognosen håller exakt; utsmetad import ger marginal. |
| **Leveransfel** | Solis kan vägra eller klippa en begärd laddeffekt. Är hela budgeten lagd i ett kvart finns ingen återhämtning. |
| **Huvudsäkringen** | 14 kW är ett hårt abonnemangstak. Ett enskilt kvart nära taket ger noll marginal för en samtidig spis eller värmepumpsstart. |
| **Enpunktsfel** | Ett enda kvart är en enda felkälla. Fyra mindre kvartar ger fyra chanser. |

**Kravet är att kvartsgränsen varierar mellan kvartarna i innevarande timme**, beroende på
det **verkliga effektmedelvärdet upp till "nu"** och det prognostiserade medeleffektbehovet
under resten av timmen — alltså inte ett fast tal utan något som räknas fram varje kvart. Ett
kvart som hittills gått snålt ska få mer utrymme än ett som gått dyrt.
**Kvartsgränsen för framtida kvartar efter innevarande timme är samma som timgränsen.**

⚠️ **Detta är ännu bara delvis byggt** — koden har ett *statiskt* kvartstak inom timmen
(`p_thresh · 0,25` per kvart). Se §10.2 punkt 1.

Att kvartstaket är ekonomiskt **gratis** framgår av att det inte ändrar timmedelvärdet: det
flyttar bara import inom timmen. Det kostar först när det tvingar fram import i ett *dyrare*
kvart än det billigaste — och då är kostnaden priset för robustheten.

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
- **Blockets energi är 3 × 5,5 = 16,5 kWh, medan ordern heter "10 kWh".** Etiketten är
  ärvd från en tidigare dimensionering och stämmer inte med vad som faktiskt bokas.
  Antingen byts namnet eller blocklängden — tills dess ska 16,5 kWh vara det tal som
  används i beräkningar.
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

Punkter där specifikationen kräver något som `src/` ännu inte har, eller där koden gör
något specen inte beskriver. **Detta är implementationsuppgifter, inte oklarheter i
specifikationen.**

### 10.1 Stängda gap (2026-11-13)

| # | Gap | Åtgärd |
|---|---|---|
| 1 | **`P_max` saknades.** Specen kräver "hittills högsta faktiska medeleffekt under en klocktimma i innevarande tariffönster". | **Byggt** i `src/peak_tracker.py`. Läser `P1/P2/P3` från HA (`input_number.batterycontrol_p1..p3`), beräknar dagens `P_max` ur nätmätarens historik och skriver tillbaka när en avslutad tariff­timme på nytt dygn slår en befintlig månadstopp. `optimize_v2` tar `p_max_today_kw` och använder den bara för dagens datum — morgondagens tröskel är `max(P_tak, P_3)`. |
| 2 | **`P_3` var hårdkodad** till 2,49 kW. | **Byggt** — samma modul. `main()` hämtar `p3_hist` via `peak_tracker.update_and_get_peaks()`. |
| 3 | **`p_eft_prognos` fanns i koden men inte i specen**, och användes som tröskelterm. | **Återinförd 2026-11-17** efter att ha varit borttagen 13–17 nov. `P_tak = max(P_mån-min, P_3, P_max, p_eft_prognos)`. Borttagningen var för bred: argumentet gäller bara när prognosen är *enda* tröskeln (§4.4). |
| 4 | **`hour_len_h`-bugg:** tariff- och 13,5 kW-raden dividerade med *antalet kvartar som återstod* (`len(quarters) * 0.25`), inte med 1,0 h, och saknade `E_nu` i högerledet. Vid `:45` blev taket fyra gånger för högt. | **Rättat 2026-11-13.** Divisorn är alltid 1,0 h; `E_nu` (redan importerad energi i timmen) ligger i högerledet på både 13,5 kW-raden och tariffraden (§4.5). **13,5 kW-raden fick slack 2026-11-17** (90 kr/kW, §4.5). |
| 5 | **"Rädda timmen" bar logiken** i stället för prisfunktionen. | **Raden är borttagen 2026-11-17** (§4.7). Prisfunktionen hanterar pågående timme själv via `E_nu`, och överträdelser prissätts i stället för att göra modellen omöjlig. |

### 10.2 Kvarvarande gap

1. **Kvartsgränserna i §4.8 är bara delvis byggda.** Det finns ett hårt kvartstak
   (`p_thresh * 0.25` per kvart i tariff­timmen), men det är **statiskt inom timmen**.
   Specen (§4.8) säger att gränsen ska **variera mellan kvartarna** beroende på verkligt
   medelvärde upp till "nu" och prognostiserat behov resten av timmen — dvs ett kvart som
   redan gått snålt ska få mer utrymme än ett som gått dyrt. Det är inte byggt.

2. **Taket är per dag i koden, per effektfönster i specen.** Koden lägger en
   `P_peak_excess`-variabel och ett `p_thresh` **per tariffdag**. Specen säger att *samma*
   timgräns gäller för alla timmar framåt inom ett effektfönster. Vid horisonter som spänner
   över dygnsgränsen behöver detta stämmas av.

3. **Prisfunktionen är en slack-variabel i koden, en ramp i specen.** Matematiskt ger de
   samma marginalkostnad (45 kr per kW överstigande timmedel), men formuleringarna skiljer
   sig. Värt att verifiera att de ger identiska planer.

4. **Morgondagens tröskel är inte `min(P2, max(dagens topp, P3))`.** Koden använder
   `max(P_tak, P_3)` för alla icke-dagens datum. Det är konservativt (kan överskatta
   morgondagens kostnad), men enkelt och LP-representerbart. Exakt rullande P3 kräver
   min/max-komposition som inte är LP-representerbar.

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
| **Effektavgift 135 kr/kW·månad för 3 högsta topparna** | **Kvar** — 45 kr/kW × 3 toppar = 135 kr/kW (§2.3 regel 5). |
| **"Utan behov av separata heuristiska regler"** | **Kvar** som princip, §1 och §4.1. |
| **Kvar att fastställa: verkningsgrader** | **Avgjort** — 0,95/0,95. |
| **Kvar att fastställa: cykelkostnadens fördelning** | **Avgjort** — 0,15 in / 0,15 ut. |
| **Kvar att fastställa: val av lösare** | **Avgjort** — HiGHS. |
| **Kvar att fastställa: testplan mot historiska 2025-data** | **Kvar**, §8. |
| **Kvartstaket** (fanns inte i version 1) | **Nytt i version 2** — men omklassat: det är ett **risktak** för robusthet (§4.8), inte en tariffregel. Det ändrar inte timmedelvärdet och är därför ekonomiskt gratis så länge det inte tvingar import till ett dyrare kvart. |
| **`P_max` i tröskeln** (fanns inte i version 1) | **Nytt i version 2** (§4.4) — en redan tagen topp är sjunket pris. |
| **`E_nu` i timmedeleffektsraderna** (fanns inte i version 1) | **Nytt 2026-11-13** (§4.5). Utan den mäts en pågående klocktimme mot de kvartar som råkar återstå, inte mot 1,0 h. |
| **Prognosmodellen** (fanns inte i version 1) | **Nytt i version 2** (§3.9) — EWMA-bias ±1,5 kW, värmekoefficient, Solcast/SMHI/NIBE. |
