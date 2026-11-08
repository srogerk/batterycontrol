#!/usr/bin/env python3
"""
Genererar en dedikerad gransknings-dashboard för Optimizer v2.
Visar:
1. Konsumenter (kW): Hushållslast + Batteriladdning + Elbilsladdning (om aktiv)
2. Producenter & Källor (kW): Nätimport + Solproduktion + Batteriurladdning
3. Batteri SOC (%) vs Inköpspris (kr/kWh) & Effekttarifftröskel

Sparar lokalt i web/index_v2.html och synkar till HA under /config/www/batterycontrol/index_v2.html
utan att påverka produktionsdashboarden.
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
import src.optimizer_v2 as opt2

LOCAL_DIR = "/workspace/projects/Batteristyrning - effektavgift/web"
HA_HOST = "10.12.22.12"
HA_TARGET_DIR = "/config/www/batterycontrol"
SSH_KEY = "/workspace/id_ed25519"
TZ_STOCKHOLM = zoneinfo.ZoneInfo("Europe/Stockholm")

def run_v2_and_build_data(ev_ordered=False):
    client = opt.HAPriceAndStateClient()

    # Aktuellt SOC från Solis
    soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")
    soc_pct = float(soc_state.get("state", 20.0)) if soc_state else 20.0
    soc_kwh = (soc_pct / 100.0) * opt2.NOMINAL_CAPACITY_KWH

    volt_state = client.get_state("sensor.solis_s6_eh1p_battery_voltage")
    backup_state = client.get_state("number.solis_s6_eh1p_backup_soc")

    load_state = client.get_state("sensor.batterycontrol_load_forecast_now")
    pv_state = client.get_state("sensor.batterycontrol_pv_forecast_now")

    load_forecast = load_state["attributes"]["forecast_48h_kw"] if load_state else [0.0]*192
    pv_forecast = pv_state["attributes"]["forecast_48h_kw"] if pv_state else [0.0]*192

    now_local = datetime.datetime.now(TZ_STOCKHOLM)
    minute = (now_local.minute // 15) * 15
    base = now_local.replace(minute=minute, second=0, microsecond=0)
    timestamps = [base + datetime.timedelta(minutes=15 * i) for i in range(192)]

    p_in, p_out, valid_timestamps = opt.build_price_vector(client, timestamps)
    T = len(valid_timestamps)
    load_f = load_forecast[:T]
    pv_f = pv_forecast[:T]

    client.close()

    plan, status, cost, day_eft_prognos = opt2.optimize_v2(
        soc_now_kwh=soc_kwh,
        load_forecast_kw=load_f,
        pv_forecast_kw=pv_f,
        p_in=p_in,
        p_out=p_out,
        timestamps=valid_timestamps,
        p3_historical_kw=2.0,
        ev_charge_ordered=ev_ordered,
        ev_remaining_hours=3.0
    )

    if not plan:
        print(f"Optimering v2 misslyckades: {status}")
        return None

    # Beräkna nyckeltal
    cost_with_battery = 0.0
    cost_without_battery = 0.0
    total_charge_kwh = 0.0
    total_discharge_kwh = 0.0
    total_ev_kwh = 0.0
    max_grid_kw = 0.0
    max_grid_time = ""

    quarter_data = []
    for i, q in enumerate(plan):
        imp = q["grid_import_kw"]
        exp = q["grid_export_kw"]
        chg = q["grid_charge_kw"]
        dis = q["discharge_kw"]
        load = q["load_kw"]
        pv = q["pv_kw"]
        ev = q["ev_grid_kw"]
        dt = datetime.datetime.fromisoformat(q["time"])

        if imp > max_grid_kw:
            max_grid_kw = imp
            max_grid_time = q["time_str"]

        cost_with_battery += float(imp * 0.25 * q["price_in"] - exp * 0.25 * q["price_out"])
        
        # Baseline utan batteri: huslast + ev - pv direkt mot nätet
        net_l = (load + ev - pv) * 0.25
        if net_l > 0:
            cost_without_battery += float(net_l * q["price_in"])
        else:
            cost_without_battery -= float((-net_l) * q["price_out"])

        total_charge_kwh += chg * 0.25
        total_discharge_kwh += dis * 0.25
        total_ev_kwh += ev * 0.25

        quarter_data.append({
            "time": q["time"],
            "time_str": q["time_str"],
            "hour": q["hour"],
            "minute": q["minute"],
            "day_name": dt.strftime("%A"),
            "day_str": dt.strftime("%a %d %b"),
            "price_in": q["price_in"],
            "price_out": q["price_out"],
            "soc_kwh": q["soc_kwh"],
            "soc_pct": q["soc_pct"],
            "soc_end_kwh": q.get("soc_end_kwh", q["soc_kwh"]),
            "soc_end_pct": q.get("soc_end_pct", q["soc_pct"]),
            "charge_kw": q["grid_charge_kw"],
            "discharge_kw": q["discharge_kw"],
            "grid_import_kw": q["grid_import_kw"],
            "grid_export_kw": q["grid_export_kw"],
            "load_kw": q["load_kw"],
            "pv_kw": q["pv_kw"],
            "ev_grid_kw": q["ev_grid_kw"],
            "tariff_threshold_kw": q.get("tariff_threshold_kw")
        })

    battery_wear_cost = float(total_discharge_kwh * 0.15)
    gross_profit = cost_without_battery - cost_with_battery
    net_profit = gross_profit - battery_wear_cost

    prognos_serializable = {}
    for d, info in day_eft_prognos.items():
        prognos_serializable[str(d)] = {
            "p_tak": info["p_tak"],
            "p_eft_prognos": round(float(info["p_eft_prognos"]), 2),
            "p_threshold": round(float(info["p_threshold"]), 2)
        }

    return {
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v2 (Specifikation 2026-11)",
        "ev_mode": "3 timmar elbil inbokad (22:00-07:00)" if ev_ordered else "Utan elbil",
        "horizon_hours": round(len(plan) / 4.0, 1),
        "horizon_quarters": len(plan),
        "current": {
            "soc_percent": soc_pct,
            "voltage": float(volt_state.get("state", 0.0)) if volt_state else 0.0,
            "backup_soc": int(float(backup_state.get("state", 15.0))) if backup_state else 15,
            "mode_text": "v2 Granskningsläge (HiGHS LP)"
        },
        "metrics": {
            "gross_profit_kr": round(gross_profit, 2),
            "net_profit_kr": round(net_profit, 2),
            "cost_with_battery_kr": round(cost_with_battery, 2),
            "cost_without_battery_kr": round(cost_without_battery, 2),
            "battery_wear_cost_kr": round(battery_wear_cost, 2),
            "total_charge_kwh": round(total_charge_kwh, 1),
            "total_discharge_kwh": round(total_discharge_kwh, 1),
            "total_ev_kwh": round(total_ev_kwh, 1),
            "max_planned_grid_kw": round(max_grid_kw, 2),
            "max_grid_time_str": max_grid_time,
            "tariff_days": prognos_serializable
        },
        "quarters": quarter_data
    }

def build_html(data_no_ev, data_ev):
    json_no_ev = json.dumps(data_no_ev)
    json_ev = json.dumps(data_ev)

    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BatteryControl v2 Granskning</title>
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
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    body {{ background: var(--bg); color: var(--text); padding: 16px; line-height: 1.5; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid var(--card-border); }}
    .title-area h1 {{ font-size: 1.4rem; font-weight: 700; color: var(--accent); }}
    .btn-toggle {{ background: var(--card-bg); border: 1px solid var(--accent); color: var(--accent); padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; }}
    .btn-toggle.active {{ background: var(--accent); color: var(--bg); }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }}
    .kpi-card {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 12px; }}
    .kpi-card .label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; }}
    .kpi-card .value {{ font-size: 1.3rem; font-weight: 700; color: var(--text); margin-top: 4px; }}
    .kpi-card .sub {{ font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; }}
    .chart-section {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
    .chart-title {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 12px; color: var(--text); }}
    .chart-wrapper {{ position: relative; height: 320px; width: 100%; }}
    .tag-chg {{ color: var(--purple); font-weight: 600; }}
    .tag-dis {{ color: var(--success); font-weight: 600; }}
    .tag-ev {{ color: #fb923c; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--card-border); }}
    th {{ color: var(--text-muted); font-weight: 600; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="title-area">
      <h1>BatteryControl v2 Granskning</h1>
      <div style="font-size: 0.85rem; color: var(--text-muted); margin-top: 4px;">
        Genererad: <span id="genTime">{data_no_ev['generated_at']}</span> | Horisont: {data_no_ev['horizon_hours']} timmar ({data_no_ev['horizon_quarters']} kvartar)
      </div>
    </div>
    <div style="display: flex; gap: 8px;">
      <button id="btnNoEv" class="btn-toggle active" onclick="switchMode(false)">Utan elbil</button>
      <button id="btnEv" class="btn-toggle" onclick="switchMode(true)">Med 3 h elbil (22-07)</button>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="label">Aktuellt SOC</div>
      <div class="value">{data_no_ev['current']['soc_percent']:.1f} %</div>
      <div class="sub">{data_no_ev['current']['soc_percent']*0.25:.1f} kWh / 25 kWh nominellt</div>
    </div>
    <div class="kpi-card">
      <div class="label">Netto besparing (horisont)</div>
      <div class="value" id="kpiNetProfit">{data_no_ev['metrics']['net_profit_kr']} kr</div>
      <div class="sub">efter slitagekostnad (15 öre/kWh ut)</div>
    </div>
    <div class="kpi-card">
      <div class="label">Max Nättopp (kW)</div>
      <div class="value" id="kpiMaxGrid">{data_no_ev['metrics']['max_planned_grid_kw']} kW</div>
      <div class="sub" id="kpiMaxGridTime">{data_no_ev['metrics']['max_grid_time_str']}</div>
    </div>
    <div class="kpi-card">
      <div class="label">Effekttariff Måndag</div>
      <div class="value">P_tak 1.3 kW</div>
      <div class="sub">P_tröskel = 2.00 kW (P3-referens)</div>
    </div>
  </div>

  <!-- Graf 1: Konsumenter -->
  <div class="chart-section">
    <div class="chart-title">1. Konsumenter (kW) — Hushållslast, Batteriladdning & Elbil</div>
    <div class="chart-wrapper">
      <canvas id="consumersChart"></canvas>
    </div>
  </div>

  <!-- Graf 2: Producenter & Källor -->
  <div class="chart-section">
    <div class="chart-title">2. Producenter & Källor (kW) — Nätimport, Solproduktion & Batteriurladdning</div>
    <div class="chart-wrapper">
      <canvas id="producersChart"></canvas>
    </div>
  </div>

  <!-- Graf 3: SOC & Inköpspris -->
  <div class="chart-section">
    <div class="chart-title">3. Batteri State of Charge (SOC %) vs Inköpspris (kr/kWh)</div>
    <div class="chart-wrapper">
      <canvas id="socPriceChart"></canvas>
    </div>
  </div>

  <div class="chart-section">
    <div class="chart-title">Planerade åtgärder under perioden</div>
    <div style="overflow-x: auto; max-height: 400px;">
      <table>
        <thead>
          <tr>
            <th>Tid</th>
            <th>Åtgärd</th>
            <th>Effekt (kW)</th>
            <th>Nätimport (kW)</th>
            <th>Batteri SOC</th>
            <th>Inköpspris</th>
          </tr>
        </thead>
        <tbody id="actionsTbody"></tbody>
      </table>
    </div>
  </div>

  <script>
    const dataNoEv = {json_no_ev};
    const dataEv = {json_ev};
    let currentData = dataNoEv;

    let chart1, chart2, chart3;

    function renderCharts(D) {{
      const quarters = D.quarters;
      const labels = quarters.map(q => q.time_str);
      const houseLoad = quarters.map(q => q.load_kw);
      const chargeKw = quarters.map(q => q.charge_kw);
      const evKw = quarters.map(q => q.ev_grid_kw || 0.0);
      const gridImport = quarters.map(q => q.grid_import_kw);
      const pvKw = quarters.map(q => q.pv_kw);
      const dischargeKw = quarters.map(q => q.discharge_kw);
      const socPct = quarters.map(q => q.soc_pct);
      const priceIn = quarters.map(q => q.price_in);

      if (chart1) chart1.destroy();
      if (chart2) chart2.destroy();
      if (chart3) chart3.destroy();

      // Chart 1: Konsumenter (Stacked)
      chart1 = new Chart(document.getElementById('consumersChart'), {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{
              label: 'Hushållskonsumtion',
              data: houseLoad,
              borderColor: '#f87171',
              backgroundColor: 'rgba(248, 113, 113, 0.35)',
              borderWidth: 1.5,
              fill: 'origin',
              stepped: 'before',
              pointRadius: 0
            }},
            {{
              label: 'Batteriladdning',
              data: chargeKw,
              borderColor: '#c084fc',
              backgroundColor: 'rgba(192, 132, 252, 0.45)',
              borderWidth: 1.5,
              fill: '-1',
              stepped: 'before',
              pointRadius: 0
            }},
            {{
              label: 'Elbilsladdning',
              data: evKw,
              borderColor: '#fb923c',
              backgroundColor: 'rgba(251, 146, 60, 0.50)',
              borderWidth: 1.5,
              fill: '-1',
              stepped: 'before',
              pointRadius: 0
            }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          scales: {{
            x: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#94a3b8', maxTicksLimit: 16 }} }},
            y: {{ stacked: true, min: 0, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#94a3b8' }}, title: {{ display: true, text: 'kW', color: '#94a3b8' }} }}
          }},
          plugins: {{ legend: {{ labels: {{ color: '#f8fafc' }} }} }}
        }}
      }});

      // Chart 2: Producenter (Stacked)
      chart2 = new Chart(document.getElementById('producersChart'), {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{
              label: 'Nätimport',
              data: gridImport,
              borderColor: '#38bdf8',
              backgroundColor: 'rgba(56, 189, 248, 0.45)',
              borderWidth: 1.5,
              fill: 'origin',
              stepped: 'before',
              pointRadius: 0
            }},
            {{
              label: 'Solproduktion',
              data: pvKw,
              borderColor: '#facc15',
              backgroundColor: 'rgba(250, 204, 21, 0.55)',
              borderWidth: 1.5,
              fill: '-1',
              stepped: 'before',
              pointRadius: 0
            }},
            {{
              label: 'Batteriurladdning',
              data: dischargeKw,
              borderColor: '#34d399',
              backgroundColor: 'rgba(52, 211, 153, 0.55)',
              borderWidth: 1.5,
              fill: '-1',
              stepped: 'before',
              pointRadius: 0
            }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          scales: {{
            x: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#94a3b8', maxTicksLimit: 16 }} }},
            y: {{ stacked: true, min: 0, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#94a3b8' }}, title: {{ display: true, text: 'kW', color: '#94a3b8' }} }}
          }},
          plugins: {{ legend: {{ labels: {{ color: '#f8fafc' }} }} }}
        }}
      }});

      // Chart 3: SOC & Inköpspris
      chart3 = new Chart(document.getElementById('socPriceChart'), {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{
              label: 'Batteri SOC (%)',
              data: socPct,
              borderColor: '#22c55e',
              backgroundColor: 'rgba(34, 197, 94, 0.1)',
              borderWidth: 2.2,
              yAxisID: 'y_soc',
              tension: 0,
              fill: false,
              pointRadius: 0
            }},
            {{
              label: 'Inköpspris (kr/kWh)',
              data: priceIn,
              borderColor: '#ef4444',
              borderWidth: 1.8,
              yAxisID: 'y_price',
              stepped: 'before',
              pointRadius: 0
            }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          scales: {{
            x: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#94a3b8', maxTicksLimit: 16 }} }},
            y_soc: {{ type: 'linear', position: 'left', min: 0, max: 100, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#22c55e' }}, title: {{ display: true, text: 'SOC (%)', color: '#22c55e' }} }},
            y_price: {{ type: 'linear', position: 'right', min: 0, grid: {{ drawOnChartArea: false }}, ticks: {{ color: '#ef4444' }}, title: {{ display: true, text: 'kr/kWh', color: '#ef4444' }} }}
          }},
          plugins: {{ legend: {{ labels: {{ color: '#f8fafc' }} }} }}
        }}
      }});

      // Uppdatera tabell
      const activeQ = quarters.filter(q => q.charge_kw > 0.05 || q.discharge_kw > 0.05 || (q.ev_grid_kw && q.ev_grid_kw > 0.05));
      const tbody = document.getElementById('actionsTbody');
      tbody.innerHTML = activeQ.map(q => {{
        let tag = '';
        let eff = '';
        if (q.charge_kw > 0.05) {{ tag += '<span class="tag-chg">Laddar batt</span> '; eff += q.charge_kw.toFixed(2) + ' kW '; }}
        if (q.discharge_kw > 0.05) {{ tag += '<span class="tag-dis">Urladdar batt</span> '; eff += q.discharge_kw.toFixed(2) + ' kW '; }}
        if (q.ev_grid_kw > 0.05) {{ tag += '<span class="tag-ev">Laddar bil</span> '; eff += '(bil ' + q.ev_grid_kw.toFixed(1) + ' kW) '; }}
        return `
          <tr>
            <td><strong>${{q.time_str}}</strong></td>
            <td>${{tag}}</td>
            <td><strong>${{eff}}</strong></td>
            <td>${{q.grid_import_kw.toFixed(2)}} kW</td>
            <td>${{q.soc_pct.toFixed(1)}} % (${{q.soc_kwh.toFixed(1)}} kWh)</td>
            <td>${{q.price_in.toFixed(3)}} kr</td>
          </tr>
        `;
      }}).join('');
    }}

    function switchMode(ev) {{
      document.getElementById('btnNoEv').classList.toggle('active', !ev);
      document.getElementById('btnEv').classList.toggle('active', ev);
      currentData = ev ? dataEv : dataNoEv;

      document.getElementById('kpiNetProfit').textContent = currentData.metrics.net_profit_kr + ' kr';
      document.getElementById('kpiMaxGrid').textContent = currentData.metrics.max_planned_grid_kw + ' kW';
      document.getElementById('kpiMaxGridTime').textContent = currentData.metrics.max_grid_time_str;

      renderCharts(currentData);
    }}

    // Initiera vid start
    renderCharts(dataNoEv);
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
        local_file, f"root@{HA_HOST}:{HA_TARGET_DIR}/index_v2.html"
    ]
    subprocess.run(scp_cmd, check=True)
    print(f"Synkad till HA: {HA_TARGET_DIR}/index_v2.html")

def main():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    print("Beräknar v2 plan UTAN elbil...")
    data_no_ev = run_v2_and_build_data(ev_ordered=False)
    print("Beräknar v2 plan MED 3h elbil (22-07)...")
    data_ev = run_v2_and_build_data(ev_ordered=True)

    if not data_no_ev or not data_ev:
        print("Fel vid beräkning av data.")
        return

    html_content = build_html(data_no_ev, data_ev)
    html_path = os.path.join(LOCAL_DIR, "index_v2.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Genererade granskningssida: {html_path}")

    try:
        sync_to_ha(html_path)
    except Exception as e:
        print(f"Kunde inte synka till HA: {e}")

if __name__ == "__main__":
    main()
