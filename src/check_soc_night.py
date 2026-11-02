#!/usr/bin/env python3
"""
Nattkontroll av batteri-SOC kl 03:30.
Skickar en notis till Home Assistant (både persistent notification och mobil-push)
med grön/röd status beroende på om SOC >= 80%.
"""
import sys
import datetime

sys.path.append("/workspace/projects/Batteristyrning - effektavgift/src")
import optimizer as opt

def main():
    client = opt.HAPriceAndStateClient()
    try:
        soc_state = client.get_state("sensor.solis_s6_eh1p_battery_soc")
        soc = float(soc_state.get("state", 0.0)) if soc_state else 0.0
        
        now_str = datetime.datetime.now().strftime("%H:%M")
        
        if soc >= 80.0:
            title = "🟢 Batterikontroll: Normal laddning"
            message = f"Kl {now_str}: Batteri-SOC är {soc:.0f}% (Mål >= 80% uppnått)."
            color = "#28a745"
        else:
            title = "🔴 Batterikontroll: Låg SOC!"
            message = f"Kl {now_str}: Batteri-SOC är bara {soc:.0f}% (under målet på 80%)."
            color = "#dc3545"

        # 1. Skapa persistent notification i HA
        client.call_service("persistent_notification", "create", {
            "notification_id": "battery_soc_night_check",
            "title": title,
            "message": message
        })

        # 2. Skicka mobil-push till Rogers telefon (eb2103)
        client.call_service("notify", "mobile_app_eb2103", {
            "title": title,
            "message": message,
            "data": {
                "color": color
            }
        })
        print(f"Skickat: {title} - {message}")
    except Exception as e:
        print(f"Fel vid nattkontroll: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    main()
