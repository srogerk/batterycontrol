#!/usr/bin/env python3
import sys
import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.append("/workspace/projects/Batteristyrning - effektavgift")
import src.optimizer as opt

def generate_plan_chart(output_path="/workspace/projects/Batteristyrning - effektavgift/plan_chart.png"):
    client = opt.HAPriceAndStateClient()
    plan_state = client.get_state("sensor.batterycontrol_plan_now")
    load_state = client.get_state("sensor.batterycontrol_load_forecast_now")
    pv_state = client.get_state("sensor.batterycontrol_pv_forecast_now")

    if not plan_state or "plan_48h" not in plan_state.get("attributes", {}):
        print("Ingen plan hittades i HA.")
        client.close()
        return None

    plan = plan_state["attributes"]["plan_48h"]
    load_forecast = load_state["attributes"]["forecast_48h_kw"] if load_state else [0.0]*len(plan)
    pv_forecast = pv_state["attributes"]["forecast_48h_kw"] if pv_state else [0.0]*len(plan)

    times = [datetime.datetime.fromisoformat(p["datetime"]) for p in plan]
    p_in, p_out, valid_times = opt.build_price_vector(client, times)
    client.close()

    soc_pct = [p["soc_percent"] for p in plan]
    chg_kw = [p["charge_kw"] for p in plan]
    dis_kw = [p["discharge_kw"] for p in plan]
    grid_imp_kw = [p["grid_import_kw"] for p in plan]
    load_kw = load_forecast[:len(plan)]
    pv_kw = pv_forecast[:len(plan)]

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(15, 10), sharex=True, gridspec_kw={'height_ratios': [1.2, 1.8, 1.2]})

    # --- Subplot 1: Inköps- och Exportpris ---
    ax1.plot(times, p_in, label="Inköpspris (kr/kWh)", color="#d9534f", linewidth=1.8)
    ax1.plot(times, p_out, label="Exportpris (kr/kWh)", color="#f0ad4e", linestyle="--", linewidth=1.2)
    ax1.set_ylabel("kr / kWh", fontsize=11, fontweight='bold')
    ax1.set_title("Optimerad Batteri- och Effektplan (Rullande Horisont)", fontsize=14, fontweight='bold', pad=12)
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(loc="upper left", framealpha=0.9)

    # --- Subplot 2: Effektflöden ---
    ax2.plot(times, load_kw, label="Huslast Prognos (kW)", color="#333333", linewidth=1.5, linestyle=":")
    ax2.plot(times, grid_imp_kw, label="Nätimport Plan (kW)", color="#0275d8", linewidth=2.0)
    ax2.fill_between(times, 0, chg_kw, label="Batteriladdning (kW)", color="#5cb85c", alpha=0.5, step="post")
    ax2.fill_between(times, 0, dis_kw, label="Batteriurladdning (kW)", color="#f0ad4e", alpha=0.5, step="post")
    if any(k > 0.05 for k in pv_kw):
        ax2.plot(times, pv_kw, label="Solprognos (kW)", color="#ffbb33", linewidth=1.5)
    
    # Markera 14 kW nätgräns
    ax2.axhline(14.0, color="#d9534f", linestyle="--", alpha=0.7, label="Nättak (14 kW)")
    ax2.set_ylabel("Effekt (kW)", fontsize=11, fontweight='bold')
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(loc="upper left", ncol=3, framealpha=0.9)

    # --- Subplot 3: Batteri State of Charge (SOC) ---
    ax3.plot(times, soc_pct, label="Batteri SOC (%)", color="#2e7d32", linewidth=2.2)
    ax3.axhline(100.0, color="#888888", linestyle=":", alpha=0.5)
    ax3.axhline(15.0, color="#d9534f", linestyle="--", alpha=0.7, label="Reserv 20:00 (15% / 3 kWh)")
    ax3.set_ylabel("SOC (%)", fontsize=11, fontweight='bold')
    ax3.set_ylim(0, 105)
    ax3.grid(True, linestyle=":", alpha=0.6)
    ax3.legend(loc="upper left", framealpha=0.9)

    # Format x-axis
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%a %H:%M'))
    ax3.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 4, 8, 12, 16, 20]))
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(output_path, dpi=130)
    plt.close()
    print(f"Graf sparad till: {output_path}")
    return output_path

if __name__ == "__main__":
    generate_plan_chart()
