#!/usr/bin/env python3
"""
Sökvägsupplösning som fungerar både som cron-jobb i utvecklingscontainern
och som HA-add-on.

Problemet: all kod hade `/workspace/projects/Batteristyrning - effektavgift`
hårdkodat i sys.path och i persistensfilerna. Den sökvägen finns inte i en
add-on (där ligger koden i /app) och /data är den enda skrivbara ytan som
överlever en uppdatering av add-onen.

Regler:
  BASE_DIR  = katalogen som innehåller src/ — härledd ur __file__, aldrig hårdkodad
  DATA_DIR  = /data om det är skrivbart (add-on), annars BASE_DIR/web (utveckling)
  CONFIG_DIR= /config om det finns (add-on med map: config:rw), annars None

Miljövariabler:
  BATTERYCONTROL_DATA_DIR   överstyr DATA_DIR
  BATTERYCONTROL_CONFIG_DIR överstyr CONFIG_DIR
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
WEB_DIR = os.path.join(BASE_DIR, "web")


def _writable(path):
    try:
        return os.path.isdir(path) and os.access(path, os.W_OK)
    except OSError:
        return False


def is_addon():
    """True när vi körs inuti en HA-add-on (Supervisor-token finns)."""
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def data_dir():
    override = os.environ.get("BATTERYCONTROL_DATA_DIR")
    if override:
        return override
    if _writable("/data"):
        return "/data"
    return WEB_DIR


def config_dir():
    override = os.environ.get("BATTERYCONTROL_CONFIG_DIR")
    if override:
        return override
    if os.path.isdir("/config"):
        return "/config"
    return None


def ensure_data_dir():
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def data_file(name):
    return os.path.join(ensure_data_dir(), name)


def add_src_to_path():
    """Ersätter de hårdkodade sys.path.append(...)-raderna."""
    import sys
    for p in (BASE_DIR, SRC_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)
