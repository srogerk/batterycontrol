# Batteristyrning – effektavgift

**Mål:** hålla importtopparna under taket när Göteborg Energis effektavgift börjar
räknas **mån 2/11-2026 07:00** (mätstart 1/11, som är söndag/lågpris), utan att
förlora spot-flexibiliteten (bastu, EV-laddning på billiga timmar).

**Status (2026-10-27):** analys + sim + spec klara; Lager 0 (peak-shaving 13 kW)
verifierat — klampar importen på ~12,85 kW även under batteriladdning (TOU-vägen).
Nästa: urladdningstest (regulatorns sista handtag), morgon-x-skript i skuggläge,
live 2/11.

## Innehåll

- `GLOBAL-OPTIMERING-MODELL.md` — **Global optimeringsmodell (48h MPC)**:
  Matematisk specifikation för LP/MILP över 192 kvartar (48 timmar), rullande omplanering
  varje kvart. Hanterar separata energiflöden, spotpriser, verkningsgrader, slitagekostnad,
  terminalvärde på SOC och 14 kW timmedelbegränsning, samt framtida integration av effekttariffen.
- `SPEC-effektregulator.md` — **huvuddokumentet för heuristisk regulator**: avtalsregler, 3-lagersarkitektur,
  x-algoritmen, aktuatorer/sensorer, verifieringsplan, GE-svar.
- `el_2025_tim.csv` — timvis import 2025 (brutto), underlag för allt.
- `blocks_classified.csv` / `blocks_42.csv` — EV-block (101 klassade, 2 564 kWh).
- `daily.csv`, `hourly_flags.csv`, `dayA_pre_batt.csv`, `temp_2025_gbg.csv` —
  dags-/timdata, flaggor, temperatur (Göteborg).
- `fig1–5` — heatmap, månadskostnad, flatten-sim, behov-vs-temp, EV-karta.

## Beslut & fakta (korta)

- Effektavgift **135 kr/kW·mån** = medlet av 3 högsta **dygnestoppar** på 3 olika
  dygn, **bruttoimport**, högpris = helgfria vardagar 07–20 (1 nov–31 mars).
- 14 kW effektnivå (timmedeleffekt) hela året; >3 överträffar/12 mån → tvångsbyte
  till 43 kW-modell. 2025 års beteende = 5 dygn >14 kW → hade gett byte i november.
- **RC-force förbjuden som laddväg** (slåss mot peak-shaving, 18 kW-kaos 21/10).
  Bara TOU-slotar. `time.set_value` (inte `set_time`). En skrivare per fönster
  (pausa Predbat när man styr manuellt).
- x-algoritmen: lös x ur Σ max(0, behov−x) = B_eff; sweet spot x 1,0–2,0;
  adaptiv ≈ 1 000 kr/säsong.

## Nästa steg

1. **Urladdningstest** — vilket register styr urladdningen 07–20? (hypotes:
   dynamiskt peak-shaving-tak = x gör jobbet)
2. **Morgon-x-skript** i skuggläge (loggar beslut, inga skrivningar).
3. **Live mån 2/11 06:55** med enkel x (månadens p90 + temperatur), kalibrera löpande.
4. Öppen fråga GE: är 24/31 dec helgfria vardagar? (tills vidare: behandlas som
   avgiftsskyddade — säkert val)

*Uppdatera med UPDATE-rader, rewrita inte historien.*
