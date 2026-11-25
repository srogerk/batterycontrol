# Entitetsstyrning av Solis — enda tillåtna vägen

**Beslut av Roger 2026-11-23 01:15:** Olle styr växelriktaren **enbart via Home
Assistant-entiteter** (`number.set_value`, `switch.turn_on/off`, `time.set_value`).
Inga råa register-skrivningar (`solis_write_holding_register`) och ingen
RC-tjänst (`solis_force_battery_charge`) som styrväg.

Skäl: råa skrivningar gav hela natten 23 nov noll resultat medan det verkliga
felet satt i **`battery_charge_limit_power` = 0** — en entitet som redan fanns.

## Skrivbara entiteter (verifierade 2026-11-23 01:15)

### Effektgränser — de viktigaste
| Entitet | Enhet | Värde då | Anm. |
|---|---|---|---|
| `number.solis_s6_eh1p_battery_charge_limit_power` | W (0–655350) | **10000** | **VAR 0 — blockerade all nätladdning** |
| `number.solis_s6_eh1p_battery_discharge_limit_power` | W | 0 | 0 blockerar INTE urladdning (se fällan) |
| `number.solis_s6_eh1p_max_charge_current` | A | 50.0 | = 43012; enheten reverte­rar skrivningar |
| `number.solis_s6_eh1p_battery_max_charge_current` | A | 50.0 | spegel av samma register |

### TOU slot 1 — nätladdningsvägen
| Entitet | Värde |
|---|---|
| `switch.grid_time_of_use_charging_period_1` | on |
| `switch.allow_grid_to_charge_the_battery` | on (bit 5) |
| `number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_1` | 30.0 A |
| `number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_1` | 100 % |
| `time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_1` | 00:45 |
| `time.solis_s6_eh1p_grid_time_of_use_charge_end_slot_1` | 05:45 |

### Golv / reserv
| Entitet | Värde |
|---|---|
| `number.solis_s6_eh1p_backup_soc` | 15 |
| `number.solis_s6_eh1p_overdischarge_soc` | 14 (BMS-ägd, rör ej) |

### Läsning (bevis, aldrig skrivning)
`sensor.solis_s6_eh1p_battery_charge_power`, `..._battery_discharge_power`,
`..._battery_soc`, `..._meter_total_active_power` (negativ = import),
`..._grid_time_of_use_charge_battery_current_slot_1`.

## Fällan: 0 betyder inte alltid "av"

`battery_discharge_limit_power` = 0 **och urladdning fungerar** (907 W uppmätt
23 nov 00:56). Alltså är "0 = hårt 0 W-tak" inte enhetligt. Att
`charge_limit_power` = 0 blockerade laddning är ändå belagt av ett
naturligt experiment:

- Timmarna innan: TOU-ström 15,2 A → **0 W laddning**
- 00:58:53 `charge_limit_power` 0 → 10 000 W
- 00:59:52: **samma 15,2 A** gav 2 406 W
- 01:01:14 TOU-ström 15,2 → 30,0 A ⇒ 8,3 kW

Den enda variabeln som ändrades mellan 0 W och 2,4 kW var 43130. Det är beviset.

## Rogers mekanism (2026-11-23 01:41) — VARFÖR TOU gav 0 W

Roger själv, efter att ha fått laddningen att starta:

> "För att få igång TOU var jag tvungen att ändra operation_mode till Self-use
> (utan reserve/backup). Det fungerar även med Self-use + reserve/backup, men då
> måste peak-shaving-mode vara av."

**Verifierat i registret 01:41 — laddar 8,3 kW, SOC 38:**

| | värde | bitar |
|---|---|---|
| 43110 under natten (0 W) | 305 | 0, 4, 5, **8** |
| 43110 nu (8,3 kW) | **49** | 0, 4, 5 |

**Enda skillnaden: bit 8 (Forcecharge Peakshaving) är borta.** Bit 4 (Reserve)
är kvar, bit 5 (Grid Charge) är kvar. Alltså:

- **Peak-shaving-modifieraren (bit 8) blockerade TOU-laddningen.**
- Reserve/backup behöver inte tas bort — bara peak-shaving.
- Det är samma mönster som [[peak-shaving-clamps-tou-charge]]: bit 8 stryper
  laddströmmen. Men i natt strypte den till **noll**, inte till "cap − hus".
  Varför skillnaden (klamp 13 kW vs 0 W) är inte klarlagd.

⚠️ **Detta motsäger den tidigare noden `peakshaving-switch-does-not-block-tou-charge`**
(2026-10-21: "switchen är INTE blockern"). Den slutsatsen drogs med en annan
konfiguration — sannolikt utan den samtidiga kombinationen av bit 8 + 43130 = 0.
Noden behöver omprövas, inte citeras som sanning.

## Kvarstående motsägelse
`battery_charge_limit_power` = 0 fanns även under de lyckade laddningarna
20–21 okt (~10 kW). Förklaringen är inte klarlagd. En möjlighet: entitetens `0`
var då en **fallback vid misslyckad Modbus-läsning** (historiken visar idel
`unavailable`-par), inte ett faktiskt registervärde. Rå registret lästes till 0
först 23 nov ~00:53 — innan dess aldrig verifierat i registret.
