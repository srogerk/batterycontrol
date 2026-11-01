#!/usr/bin/env python3
"""
Genererar en modern, interaktiv status- och dashboard-sida för BatteryControl.
Sidan och tillhörande data sparas lokalt och synkas automatiskt till Home Assistant
under /config/www/batterycontrol/ så att den kan visas direkt i HA via en Webpage-card
eller i sidopanelen via iframe (URL: /local/batterycontrol/index.html).
"""

import sys
import os
import json
import datetime
import zoneinfo
import subprocess
import numpy as np

sys.path.append("/workspace/projects/Batteristyrning - effektavgift")
import src.optimizer as opt

LOCAL_DIR = "/workspace/projects/Batteristyrning - effektavgift/web"
HA_HOST = "10.12.22.12"
HA_TARGET_DIR = "/config/www/batterycontrol"
SSH_KEY = "/workspace/id_ed25519"
TZ_STOCKHOLM = zoneinfo.ZoneInfo("Europe/Stockholm")

def fetch_and_compute():
    client = opt.HAPriceAndStateClient()
    plan_state = client.get_state("sensor.batterycontrol_plan_now")
    status_state = client.get_state("sensor.batterycontrol_status")
    load_state = client.get_state("sensor.batterycontrol_load_forecast_now")
    pv_state = client.get_state("sensor.batterycontrol_pv_forecast_now")
    soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")
    volt_state = client.get_state("sensor.solis_s6_eh1p_battery_voltage")
    backup_state = client.get_state("number.solis_s6_eh1p_backup_soc")
    active_bool = client.get_state("input_boolean.batterycontrol_active")
    dry_run_bool = client.get_state("input_boolean.batterycontrol_dry_run")

    if not plan_state or "plan_48h" not in plan_state.get("attributes", {}):
        print("Varning: Ingen optimeringsplan hittades i HA.")
        client.close()
        return None

    plan = plan_state["attributes"]["plan_48h"]
    load_forecast = load_state["attributes"]["forecast_48h_kw"] if load_state else [0.0]*len(plan)
    pv_forecast = pv_state["attributes"]["forecast_48h_kw"] if pv_state else [0.0]*len(plan)

    times = [datetime.datetime.fromisoformat(p["datetime"]) for p in plan]
    p_in, p_out, valid_times = opt.build_price_vector(client, times)
    client.close()

    # Ekonomiska och tekniska beräkningar över planeringshorisonten
    cost_with_battery = 0.0
    total_charge_kwh = 0.0
    total_discharge_kwh = 0.0
    cost_without_battery = 0.0
    max_planned_grid_kw = 0.0
    max_grid_time_str = ""

    quarter_data = []

    for i, p in enumerate(plan):
        imp_kw = p["grid_import_kw"]
        exp_kw = p["grid_export_kw"]
        chg_kw = p["charge_kw"]
        dis_kw = p["discharge_kw"]
        load_kw = load_forecast[i] if i < len(load_forecast) else 0.0
        pv_kw = pv_forecast[i] if i < len(pv_forecast) else 0.0

        dt_utc = times[i]
        dt_local = dt_utc.astimezone(TZ_STOCKHOLM)
        t_str = dt_local.strftime("%a %H:%M")

        if imp_kw > max_planned_grid_kw:
            max_planned_grid_kw = imp_kw
            max_grid_time_str = t_str

        e_imp = imp_kw * 0.25
        e_exp = exp_kw * 0.25
        cost_with_battery += float(e_imp * p_in[i] - e_exp * p_out[i])

        chg_kwh = chg_kw * 0.25
        dis_kwh = dis_kw * 0.25
        total_charge_kwh += chg_kwh
        total_discharge_kwh += dis_kwh

        # Baseline utan batteri (direktanvändning av PV till last, resten nätimport)
        net_l = (load_kw - pv_kw) * 0.25
        if net_l > 0:
            cost_without_battery += float(net_l * p_in[i])
        else:
            cost_without_battery -= float((-net_l) * p_out[i])

        quarter_data.append({
            "time": p["datetime"],
            "time_str": t_str,
            "hour": dt_local.hour,
            "minute": dt_local.minute,
            "day_name": dt_local.strftime("%A"),
            "day_str": dt_local.strftime("%a %d %b"),
            "price_in": round(float(p_in[i]), 3),
            "price_out": round(float(p_out[i]), 3),
            "soc_kwh": float(p["soc_kwh"]),
            "soc_pct": float(p["soc_percent"]),
            "charge_kw": float(p["charge_kw"]),
            "discharge_kw": float(p["discharge_kw"]),
            "grid_import_kw": float(p["grid_import_kw"]),
            "grid_export_kw": float(p["grid_export_kw"]),
            "load_kw": round(float(load_kw), 2),
            "pv_kw": round(float(pv_kw), 2)
        })

    # Slitagekostnad: 15 öre/kWh per genomströmmad energicykel (baseras på urladdad energi)
    # Alternativt 15 öre ladd + 15 öre urladd = 30 öre fullcykel, men per Roger är det 15 öre/kWh
    battery_wear_cost = float(total_discharge_kwh * 0.15)

    # Bruttovinst = besparing direkt mot baseline utan batteri
    gross_profit = cost_without_battery - cost_with_battery

    # Terminalvärdesjustering för batteriets differens (för referens)
    soc_start = float(plan[0]["soc_kwh"])
    soc_end = float(plan[-1]["soc_kwh"])
    v_term = float(np.mean(p_in))
    term_diff = (soc_end - soc_start) * v_term

    net_profit = gross_profit - battery_wear_cost

    # Effekttariff / effektavgift nyckelvärden
    now_local = datetime.datetime.now(TZ_STOCKHOLM)
    tariff_month = (now_local.month in [11, 12, 1, 2, 3])
    
    # 07:00 målnivå i lokal tid (Europe/Stockholm)
    next_07 = [q for q in quarter_data if q["hour"] == 7 and q["minute"] == 0]
    target_07_soc = next_07[0]["soc_pct"] if next_07 else None

    # Nuvarande status
    current_soc = float(soc_state.get("state", 0.0)) if soc_state else 0.0
    current_voltage = float(volt_state.get("state", 0.0)) if volt_state else 0.0
    current_backup_soc = int(float(backup_state.get("state", 15.0))) if backup_state else 15
    is_active = (active_bool.get("state") == "on") if active_bool else False
    is_dry_run = (dry_run_bool.get("state") == "on") if dry_run_bool else True

    next_chg = float(plan[0]["charge_kw"])
    next_dis = float(plan[0]["discharge_kw"])
    next_imp = float(plan[0]["grid_import_kw"])

    data = {
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "horizon_hours": round(len(plan) / 4.0, 1),
        "horizon_quarters": len(plan),
        "current": {
            "soc_percent": current_soc,
            "voltage": current_voltage,
            "backup_soc": current_backup_soc,
            "active": is_active,
            "dry_run": is_dry_run,
            "mode_text": "Skuggläge (Dry Run)" if is_dry_run else ("Aktiv styrning" if is_active else "Inaktiv"),
            "next_charge_kw": next_chg,
            "next_discharge_kw": next_dis,
            "next_import_kw": next_imp,
            "current_spot_in": round(float(p_in[0]), 2)
        },
        "metrics": {
            "gross_profit_kr": round(gross_profit, 2),
            "net_profit_kr": round(net_profit, 2),
            "cost_with_battery_kr": round(cost_with_battery, 2),
            "cost_without_battery_kr": round(cost_without_battery, 2),
            "battery_wear_cost_kr": round(battery_wear_cost, 2),
            "terminal_adjustment_kr": round(term_diff, 2),
            "total_charge_kwh": round(total_charge_kwh, 1),
            "total_discharge_kwh": round(total_discharge_kwh, 1),
            "max_planned_grid_kw": round(max_planned_grid_kw, 2),
            "max_grid_time_str": max_grid_time_str,
            "target_07_soc": target_07_soc,
            "tariff_active_season": tariff_month
        },
        "quarters": quarter_data
    }

    return data

def build_html(data):
    json_str = json.dumps(data)

    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>BatteryControl Dashboard</title>
  <!-- Chart.js för interaktiva, snabba grafer -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --purple: #a855f7;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      padding: 16px;
      line-height: 1.5;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 20px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--card-border);
    }}
    .title-area h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .title-area .subtitle {{
      color: var(--text-muted);
      font-size: 0.85rem;
    }}
    .badge {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .badge-dry {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }}
    .badge-live {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }}
    .badge-off {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }}

    /* KPI Grid */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .kpi-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .kpi-title {{
      font-size: 0.8rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 6px;
    }}
    .kpi-value {{
      font-size: 1.6rem;
      font-weight: 700;
      color: var(--text);
    }}
    .kpi-sub {{
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .kpi-val-green {{ color: var(--success); }}
    .kpi-val-accent {{ color: var(--accent); }}
    .kpi-val-warn {{ color: var(--warning); }}

    /* Chart Containers */
    .chart-section {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 20px;
    }}
    .chart-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }}
    .chart-title {{
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
    }}
    .chart-wrapper {{
      position: relative;
      height: 280px;
      width: 100%;
    }}

    /* Table preview */
    .table-section {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 16px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }}
    th {{
      color: var(--text-muted);
      font-weight: 600;
      border-bottom: 1px solid var(--card-border);
      padding: 8px;
    }}
    td {{
      padding: 8px;
      border-bottom: 1px solid rgba(255,255,255,0.05);
    }}
    tr:hover td {{
      background: rgba(255,255,255,0.03);
    }}
    .tag-chg {{ color: var(--success); font-weight: 600; }}
    .tag-dis {{ color: var(--warning); font-weight: 600; }}
  </style>
</head>
<body>

  <div class="header">
    <div class="title-area">
      <h1>⚡ BatteryControl GUI</h1>
      <div class="subtitle">Global LP-styrning med effekttaxa & spotprisarbitrage • Horisont: <span id="h-hours">--</span> h (<span id="h-q">--</span> kvartar)</div>
    </div>
    <div>
      <span id="mode-badge" class="badge">--</span>
      <span style="font-size: 0.8rem; color: var(--text-muted); margin-left: 8px;" id="gen-time">--</span>
    </div>
  </div>

  <!-- KPI Grid -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-title">Estimerad Bruttovinst</div>
      <div class="kpi-value kpi-val-green" id="kpi-profit">+0.00 kr</div>
      <div class="kpi-sub" id="kpi-profit-sub">Arbitrage vs direktförbrukning</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Batteri Status (SOC)</div>
      <div class="kpi-value kpi-val-accent" id="kpi-soc">-- %</div>
      <div class="kpi-sub" id="kpi-soc-sub">Mål 07:00: -- % | Spärr: -- %</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Börvärde Just Nu</div>
      <div class="kpi-value" id="kpi-next-flow">0.0 kW</div>
      <div class="kpi-sub" id="kpi-spot-now">Inköpspris nu: -- kr/kWh</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Planerad Nätimport Max</div>
      <div class="kpi-value kpi-val-warn" id="kpi-max-grid">0.0 kW</div>
      <div class="kpi-sub" id="kpi-max-grid-sub">Tidpunkt: --</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">OMSÄTTNING BAT In/Ut</div>
      <div class="kpi-value" id="kpi-throughput">0,0 / 0,0 kWh</div>
      <div class="kpi-sub" id="kpi-wear">Nettovinst: +0.00 kr | Slitage: 0.00 kr</div>
    </div>
  </div>

  <!-- Graf 1: Konsumenter (Stacked Area) -->
  <div class="chart-section">
    <div class="chart-header">
      <div class="chart-title">Konsumenter (kW)</div>
    </div>
    <div class="chart-wrapper">
      <canvas id="consumersChart"></canvas>
    </div>
  </div>

  <!-- Graf 2: Producenter & Källor (Stacked Area) -->
  <div class="chart-section">
    <div class="chart-header">
      <div class="chart-title">Producenter & Källor (kW)</div>
    </div>
    <div class="chart-wrapper">
      <canvas id="producersChart"></canvas>
    </div>
  </div>

  <!-- Graf 3: Batteri SOC & Inköpspris -->
  <div class="chart-section">
    <div class="chart-header">
      <div class="chart-title">Batteri State of Charge (SOC %) vs Inköpspris (kr/kWh)</div>
      <div style="font-size: 0.8rem; color: var(--text-muted);">Inköpspris och laddcykler</div>
    </div>
    <div class="chart-wrapper">
      <canvas id="socPriceChart"></canvas>
    </div>
  </div>

  <!-- Tabell över aktiva åtgärder -->
  <div class="table-section">
    <div class="chart-title" style="margin-bottom: 12px;">Planerade Batteriaktioner (Endast aktiva kvartar)</div>
    <table>
      <thead>
        <tr>
          <th>Tid</th>
          <th>Åtgärd</th>
          <th>Effekt (kW)</th>
          <th>Nätimport (kW)</th>
          <th>SOC (%)</th>
          <th>Inköpspris (kr/kWh)</th>
        </tr>
      </thead>
      <tbody id="actions-table-body">
      </tbody>
    </table>
  </div>

  <script>
    const D = {json_str};

    // Fyll KPI:er
    document.getElementById("h-hours").innerText = D.horizon_hours;
    document.getElementById("h-q").innerText = D.horizon_quarters;
    document.getElementById("gen-time").innerText = "Uppdaterad: " + D.generated_at;

    const badge = document.getElementById("mode-badge");
    if (D.current.dry_run) {{
      badge.innerText = "Skuggläge (Dry Run)";
      badge.className = "badge badge-dry";
    }} else if (D.current.active) {{
      badge.innerText = "Aktiv Styrning (Live)";
      badge.className = "badge badge-live";
    }} else {{
      badge.innerText = "Inaktiv";
      badge.className = "badge badge-off";
    }}

    document.getElementById("kpi-profit").innerText = (D.metrics.gross_profit_kr >= 0 ? "+" : "") + D.metrics.gross_profit_kr.toFixed(2) + " kr";
    document.getElementById("kpi-profit-sub").innerText = "Utan batteri: " + D.metrics.cost_without_battery_kr.toFixed(2) + " kr | Med: " + D.metrics.cost_with_battery_kr.toFixed(2) + " kr";

    document.getElementById("kpi-soc").innerText = D.current.soc_percent.toFixed(1) + " %";
    document.getElementById("kpi-soc-sub").innerText = "Mål 07:00: " + (D.metrics.target_07_soc ? D.metrics.target_07_soc.toFixed(0) + "%" : "Ej aktiv") + " | Spärr: " + D.current.backup_soc + "%";

    if (D.current.next_charge_kw > 0.05) {{
      document.getElementById("kpi-next-flow").innerHTML = '<span class="tag-chg">+' + D.current.next_charge_kw.toFixed(2) + ' kW (Laddar)</span>';
    }} else if (D.current.next_discharge_kw > 0.05) {{
      document.getElementById("kpi-next-flow").innerHTML = '<span class="tag-dis">-' + D.current.next_discharge_kw.toFixed(2) + ' kW (Urladdar)</span>';
    }} else {{
      document.getElementById("kpi-next-flow").innerText = "0.0 kW (Väntar)";
    }}
    document.getElementById("kpi-spot-now").innerText = "Inköpspris nu: " + D.current.current_spot_in.toFixed(2) + " kr/kWh";

    document.getElementById("kpi-max-grid").innerText = D.metrics.max_planned_grid_kw.toFixed(2) + " kW";
    document.getElementById("kpi-max-grid-sub").innerText = "Tidpunkt: " + D.metrics.max_grid_time_str;
    document.getElementById("kpi-throughput").innerText = D.metrics.total_charge_kwh.toFixed(1).replace(".", ",") + " / " + D.metrics.total_discharge_kwh.toFixed(1).replace(".", ",") + " kWh";
    document.getElementById("kpi-wear").innerText = "Kostnad: " + D.metrics.battery_wear_cost_kr.toFixed(2).replace(".", ",") + " kr | Nettovinst: " + (D.metrics.net_profit_kr >= 0 ? "+" : "") + D.metrics.net_profit_kr.toFixed(2).replace(".", ",") + " kr";

    // Förbered data för grafer
    const quarters = D.quarters;
    const gridImport = quarters.map(q => q.grid_import_kw);
    const houseLoad = quarters.map(q => q.load_kw);
    const chargeKw = quarters.map(q => q.charge_kw);
    const dischargeKw = quarters.map(q => q.discharge_kw);
    const pvKw = quarters.map(q => q.pv_kw);
    const gridExport = quarters.map(q => q.grid_export_kw);
    const socPct = quarters.map(q => q.soc_pct);
    const priceIn = quarters.map(q => q.price_in);

    // Tidsaxel-formatering:
    // 1. Ange bara jämna timmar med klockslag (t.ex. 13).
    // 2. Ange dag en gång placerad mellan 00 och 01 (vid 00:00).
    // 3. Om horisonten börjar efter 00 visas startdagens namn på första jämna timmen.
    const axisLabels = quarters.map((q, idx) => {{
      if (q.hour === 0 && q.minute === 0) {{
        return q.day_str;
      }}
      if (idx === 0 || (idx < 4 && q.minute === 0)) {{
        // Visa första timmen tydligt
        if (q.minute === 0) {{
          return q.hour + ":00 (" + q.day_name.substring(0,3) + ")";
        }}
      }}
      if (q.minute === 0) {{
        return String(q.hour);
      }}
      return "";
    }});

    // Gemensamma inställningar för att ta bort punkter/ringar
    const noPointConfig = {{
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHitRadius: 8
    }};

    // Gemensam X-axelkonfiguration
    const sharedXAxis = {{
      grid: {{
        color: (ctx) => {{
          const q = quarters[ctx.index];
          if (!q) return 'rgba(255,255,255,0.03)';
          if (q.hour === 0 && q.minute === 0) return 'rgba(56, 189, 248, 0.4)'; // Markera midnatt tydligt
          if (q.minute === 0) return 'rgba(255,255,255,0.08)';
          return 'transparent';
        }},
        lineWidth: (ctx) => {{
          const q = quarters[ctx.index];
          return (q && q.hour === 0 && q.minute === 0) ? 2 : 1;
        }}
      }},
      ticks: {{
        color: (ctx) => {{
          const q = quarters[ctx.index];
          if (q && q.hour === 0 && q.minute === 0) return '#38bdf8';
          return '#94a3b8';
        }},
        font: (ctx) => {{
          const q = quarters[ctx.index];
          if (q && q.hour === 0 && q.minute === 0) return {{ weight: 'bold', size: 11 }};
          return {{ size: 10 }};
        }},
        autoSkip: false,
        maxRotation: 0,
        minRotation: 0,
        callback: function(val, index) {{
          return axisLabels[index];
        }}
      }}
    }};

    // Chart 1: Konsumenter (Stacked Area)
    new Chart(document.getElementById('consumersChart'), {{
      type: 'line',
      data: {{
        labels: quarters.map(q => q.time_str),
        datasets: [
          // Nederst: Hushållskonsumtion
          {{
            label: 'Hushållskonsumtion',
            data: houseLoad,
            borderColor: '#f87171',
            backgroundColor: 'rgba(248, 113, 113, 0.35)',
            borderWidth: 1.5,
            fill: 'origin',
            stepped: 'before',
            ...noPointConfig
          }},
          // Ovanpå: Batteriladdning
          {{
            label: 'Batteriladdning',
            data: chargeKw,
            borderColor: '#c084fc',
            backgroundColor: 'rgba(192, 132, 252, 0.45)',
            borderWidth: 1.5,
            fill: '-1',
            stepped: 'before',
            ...noPointConfig
          }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        scales: {{
          x: sharedXAxis,
          y: {{
            stacked: true,
            grid: {{ color: 'rgba(255,255,255,0.05)' }},
            ticks: {{ color: '#94a3b8' }},
            title: {{ display: true, text: 'kW', color: '#94a3b8' }},
            min: 0
          }}
        }},
        plugins: {{
          legend: {{ labels: {{ color: '#f8fafc', font: {{ size: 11 }} }} }},
          tooltip: {{
            callbacks: {{
              title: function(items) {{
                const q = quarters[items[0].dataIndex];
                return q ? q.time_str + " (" + q.day_str + ")" : "";
              }}
            }}
          }}
        }}
      }}
    }});

    // Chart 2: Producenter & Källor (Stacked Area)
    new Chart(document.getElementById('producersChart'), {{
      type: 'line',
      data: {{
        labels: quarters.map(q => q.time_str),
        datasets: [
          // Nederst: Nätimport
          {{
            label: 'Nätimport',
            data: gridImport,
            borderColor: '#38bdf8',
            backgroundColor: 'rgba(56, 189, 248, 0.45)',
            borderWidth: 1.5,
            fill: 'origin',
            stepped: 'before',
            ...noPointConfig
          }},
          // Mellan: Solproduktion
          {{
            label: 'Solproduktion',
            data: pvKw,
            borderColor: '#facc15',
            backgroundColor: 'rgba(250, 204, 21, 0.55)',
            borderWidth: 1.5,
            fill: '-1',
            stepped: 'before',
            ...noPointConfig
          }},
          // Toppen: Batteriurladdning
          {{
            label: 'Batteriurladdning',
            data: dischargeKw,
            borderColor: '#34d399',
            backgroundColor: 'rgba(52, 211, 153, 0.55)',
            borderWidth: 1.5,
            fill: '-1',
            stepped: 'before',
            ...noPointConfig
          }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        scales: {{
          x: sharedXAxis,
          y: {{
            stacked: true,
            grid: {{ color: 'rgba(255,255,255,0.05)' }},
            ticks: {{ color: '#94a3b8' }},
            title: {{ display: true, text: 'kW', color: '#94a3b8' }},
            min: 0
          }}
        }},
        plugins: {{
          legend: {{ labels: {{ color: '#f8fafc', font: {{ size: 11 }} }} }},
          tooltip: {{
            callbacks: {{
              title: function(items) {{
                const q = quarters[items[0].dataIndex];
                return q ? q.time_str + " (" + q.day_str + ")" : "";
              }}
            }}
          }}
        }}
      }}
    }});

    // Chart 2: SOC & Spotpris
    new Chart(document.getElementById('socPriceChart'), {{
      type: 'line',
      data: {{
        labels: quarters.map(q => q.time_str),
        datasets: [
          {{
            label: 'Batteri SOC (%)',
            data: socPct,
            borderColor: '#22c55e',
            backgroundColor: 'rgba(34, 197, 94, 0.1)',
            borderWidth: 2.2,
            yAxisID: 'y_soc',
            tension: 0.3,
            fill: false,
            ...noPointConfig
          }},
          {{
            label: 'Inköpspris (kr/kWh)',
            data: priceIn,
            borderColor: '#ef4444',
            borderWidth: 1.8,
            yAxisID: 'y_price',
            stepped: 'before',
            ...noPointConfig
          }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        scales: {{
          x: sharedXAxis,
          y_soc: {{
            type: 'linear',
            position: 'left',
            min: 0,
            max: 100,
            grid: {{ color: 'rgba(255,255,255,0.05)' }},
            ticks: {{ color: '#22c55e' }},
            title: {{ display: true, text: 'SOC (%)', color: '#22c55e' }}
          }},
          y_price: {{
            type: 'linear',
            position: 'right',
            min: 0,
            grid: {{ drawOnChartArea: false }},
            ticks: {{
              color: '#ef4444',
              callback: function(val) {{
                return Number(val).toFixed(2).replace('.', ',') + ' kr';
              }}
            }},
            title: {{ display: true, text: 'kr / kWh', color: '#ef4444' }}
          }}
        }},
        plugins: {{
          legend: {{ labels: {{ color: '#f8fafc', font: {{ size: 11 }} }} }},
          tooltip: {{
            callbacks: {{
              title: function(items) {{
                const q = quarters[items[0].dataIndex];
                return q ? q.time_str + " (" + q.day_str + ")" : "";
              }}
            }}
          }}
        }}
      }}
    }});

    // Tabell med aktiva kvartar
    const tbody = document.getElementById("actions-table-body");
    const activeQuarters = D.quarters.filter(q => q.charge_kw > 0.05 || q.discharge_kw > 0.05);
    if (activeQuarters.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-muted); padding: 16px;">Ingen aktiv laddning eller urladdning planerad under perioden.</td></tr>';
    }} else {{
      tbody.innerHTML = activeQuarters.map(q => `
        <tr>
          <td><strong>${{q.time_str}}</strong></td>
          <td>${{q.charge_kw > 0.05 ? '<span class="tag-chg">Laddar</span>' : '<span class="tag-dis">Urladdar</span>'}}</td>
          <td><strong>${{q.charge_kw > 0.05 ? q.charge_kw.toFixed(2) : q.discharge_kw.toFixed(2)}} kW</strong></td>
          <td>${{q.grid_import_kw.toFixed(2)}} kW</td>
          <td>${{q.soc_pct.toFixed(1)}} % (${{q.soc_kwh.toFixed(1)}} kWh)</td>
          <td>${{q.price_in.toFixed(3)}} kr</td>
        </tr>
      `).join('');
    }}
  </script>
</body>
</html>
"""
    return html

def sync_to_ha(local_file):
    cmd = [
        "ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=accept-new",
        f"root@{HA_HOST}", f"mkdir -p {HA_TARGET_DIR}"
    ]
    subprocess.run(cmd, check=True)

    scp_cmd = [
        "scp", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=accept-new",
        local_file, f"root@{HA_HOST}:{HA_TARGET_DIR}/index.html"
    ]
    subprocess.run(scp_cmd, check=True)
    print(f"Synkad till HA: {HA_TARGET_DIR}/index.html")

def main():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    data = fetch_and_compute()
    if not data:
        print("Kunde inte generera dashboard-data.")
        return

    # Spara JSON lokalt
    json_path = os.path.join(LOCAL_DIR, "data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    # Spara HTML lokalt
    html_content = build_html(data)
    html_path = os.path.join(LOCAL_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Dashboard genererad lokalt: {html_path}")

    # Synka till Home Assistant /config/www/batterycontrol/index.html
    try:
        sync_to_ha(html_path)
    except Exception as e:
        print(f"Kunde inte synka till HA: {e}")

if __name__ == "__main__":
    main()
