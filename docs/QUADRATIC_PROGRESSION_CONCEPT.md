# Koncept: Kontinuerlig Kvadratisk Dämpning (QP-modell)

**Status:** Parkerad som referens och konceptgren (`concept/quadratic-progression-qp`).  
**Datum:** 2026-11-01  
**Författare:** Roger & Olle  

---

## 1. Bakgrund & Motivation

I linjär programmering (LP) med fasta eller grova trösklar kan optimeraren utnyttja lokala prisskillnader genom att ladda med full effekt (upp mot 10 kW) om prisdifferensen överstiger tröskeln, för att sedan slå av helt under efterföljande kvartar.

Ett teoretiskt sätt att eliminera diskreta trösklar helt är att låta skuggpriset för laddning öka kontinuerligt och jämnt med laddeffekten $P$:
$$\text{Skuggpris}(P) = p_{\text{spot}} \cdot \left(1 + \frac{P}{P_{\text{ref}}}\right)$$
där $P_{\text{ref}} = 10\text{ kW}$ innebär att påslaget fördubblar kostnaden per kWh vid maximal laddeffekt (10 kW).

## 2. Matematisk Formulering: QP (Quadratic Programming)

När kostnaden per kWh ökar linjärt med effekten:
$$c(P) = p_{\text{spot}} + \alpha \cdot P$$
blir den totala energikostnaden för ett tidsintervall $\Delta t = 0{,}25\text{ h}$:
$$\text{Kostnad}_t = E_t \cdot c(P_t) = (P_t \cdot \Delta t) \cdot (p_{\text{spot}, t} + \alpha \cdot P_t) = p_{\text{spot}, t} \cdot \Delta t \cdot P_t + \alpha \cdot \Delta t \cdot P_t^2$$

Målfunktionen över horisonten $T$ blir därmed:
$$\min \sum_{t=1}^T \left[ p_{\text{spot}, t} \cdot E_{\text{grid}, t} + \alpha \cdot \frac{E_{\text{grid\_batt}, t}^2}{\Delta t} + \text{slitage} + \text{effektavgift} \dots \right]$$

Detta är ett **konvext kvadratiskt optimeringsproblem (QP)**:
$$\min \frac{1}{2} x^T Q x + c^T x \quad \text{under bivillkor } A_{\text{ub}} x \le b_{\text{ub}}, \, A_{\text{eq}} x = b_{\text{eq}}$$
där matrisen $Q$ är positivt semidefinit med diagonalelement $2 \alpha / \Delta t$ för laddvariablerna.

## 3. Lösbarhet och Verktyg

HiGHS-motorn (som redan används via `scipy.optimize.linprog`) har direkt stöd för Quadratic Programming (QP). I Python kan detta formuleras antingen via:
1. `scipy.optimize.milp` / `scipy.optimize.minimize(method='trust-constr')` med Hessian.
2. Direkta HiGHS Python-bindningar (`highspy`).

## 4. Slutsats & Beslut

Modellen parkeras på denna konceptgren. I produktion och aktiv drift implementeras i stället den **styckvis linjära (progressiva) trappstegsmodellen** i LP-lösaren. Den styckvisa modellen bevarar LP-strukturen, bibehåller lösningstider under 0,08 sekunder och åstadkommer i praktiken samma kontinuerliga dämpning av toppeffekter.
