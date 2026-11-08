# Säsongsgolv för effekttak ($P_{\text{dag}}$) i batteristyrningen

## 1. Bakgrund och problemformulering

Vid Göteborg Energis tidsindelade effekttariff (högprishelgfria vardagar kl 07:00–20:00 under november–mars) debiteras 135 kr/kW baserat på medelvärdet av månadens tre högsta timtoppar på olika dagar under debiteringsfönstret.

I en rullande MPC-optimering med 48-timmars horisont uppstår ett allvarligt problem:
- Optimeraren ser inte hela månaden framåt.
- På milda dagar med låg huslast försöker optimeraren minimera den lokala dagsimporten ner mot 0 kW genom att ladda ur batteriet för att ta hela husets baslast.
- Detta leder till **onödigt cyklad energi**: batteriet slits för 0,30 kr/kWh roundtrip för att hyvla bort baslast på en dag som ändå aldrig skulle ha blivit en av månadens tre toppdagar.

## 2. Rogers marginaloptimeringsinsikt

Om vi sätter ett onödigt lågt effekttak förlorar vi på två fronter:
1. **Direkt cykelkostnad:** Batterislitaget äter upp eventuell tariffvinst. 270 timmar med 1 kW onödig urladdning kostar ca 81 kr/månad i slitage, medan dagsandelens effektvärde bara är 45 kr/kW.
2. **Alternativkostnad för sparad kapacitet:** En sparad kWh har alternativvinsten:
   $$\text{Alternativvinst} = 21 \text{ dagar} \times k \times \Delta_{\text{marginal-peakshaving}}$$
   där $k$ är andelen dagar med onödig baslast-urladdning och $\Delta_{\text{marginal-peakshaving}}$ är vinsten av att använda samma kapacitet till att kapa extrema förbrukningstoppar under månadens tyngsta dagar eller under dyra timmar (06–07 / 20–22).

## 3. Empirisk analys mot timdata (2025)

En simulering över 2025 års timdata (`el_2025_tim.csv`) där effekttaket sveptes mellan 0,5 kW och 4,0 kW visade:

| Månad | Karaktär | Teoretiskt optimum | Vald driftsnivå (Golv) | Motivering |
|---|---|---|---|---|
| **December, Januari** | Kall vinter, hög värmelast | ~1,50 kW | **1,60 kW** | Kurvan är flack; skillnaden mot teoretiskt minimum är < 2 kr/mån. 1,60 kW sparar battericykler och ger marginal mot prognosfel. |
| **November, Februari, Mars** | Mild vinter, måttlig last | ~1,25 kW | **1,30 kW** | Kostnadsökning mot optimum är endast +2 kr/mån. Skyddar batteriet från att tömmas på milda dagar. |
| **April–Oktober** | Lågprissäsong (0 kr/kW) | 0,00 kW | **0,00 kW** | Ingen effekttariff debiteras. |


## 4. Formulering av optimerarens logik

Det finns ett säsongsgolv som är fast per månad och motsvarar det förväntade medelvärdet av de 3 högsta effektopparna under månaden.
Säsongsgolvet avser det lägsta effekttaket, inget annat.
- Effektstyrningen skall sträva efter att inte förbruka mer energi än max(Pgolv,Pmax-so-far-today,P-max3-månad) * 1h under 1 och samma timma (Ex 7.00-8.00). Notera att det innebär att optimeringen måste ta hänsyn till "energi-skuld eller -överskott" som finns vid x.15, x.30 x.45 och anpassa styrningen så att innevarande timme följer optimeringsmålet. Vi har kallar denna del av styrningen för "rädda timmen". Optimeraren fördelar ingående underskott eller överskott på de kvarvarande kvartarna av timmmen som en del av den vanliga optimeringen, kompenseringen av överskottet/underskåttet blir en ändring av effektbehovet. 

## 4. Implementering i LP-optimeraren

I `src/optimizer.py` definieras funktionen `get_p_dag_floor_kw(dt)`. För varje unik tariffdag i planeringshorisonten binds optimeringsvariabeln $P_{\text{dag}}$ till:
$$\text{bounds}[P_{\text{dag}}] = (P_{\text{floor}}, \infty)$$

Detta förhindrar att dagsoptimeraren sätter ett tak under säsongsgolvet, vilket sparar batteriets energi till verkliga toppar.
