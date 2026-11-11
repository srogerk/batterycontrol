#!/usr/bin/env python3
"""
Elbilsladdblock — fast förankrat i tiden.

PROBLEMET (spec 2026-11, punkt 13–14 + implementationsbeaktande):
Den gamla koden sökte billigaste 3-timmarsfönstret mellan 22:00 och 07:00 på
nytt vid VARJE kvartsoptimering. Så länge fönstret låg framför sig i tiden var
det stabilt, men i samma stund blocket började konsumeras var dess starttid inte
längre valbar — då valdes ett helt nytt block. Fönstret "gled framför sig":

    03:00-block valt   ->  plan visar 03:00–05:45
    klockan 03:30      ->  plan visar 03:30–06:15   (bilen laddar aldrig klart)

LÖSNINGEN:
Blocket väljs EN gång, sparas i /data, och återanvänds. Vid varje kvart beräknas
bara hur många kvartar av blocket som ÅTERSTÅR — behovet krymper, fönstret ligger
fast. Blocket väljs om endast när:
  - ingen sparad bokning finns,
  - bokningen redan passerat (sluttid < nu),
  - eller beställningen gjorts om (ny order efter att förra slutförts).

Kvartsregeln: en kvart som redan påbörjats räknas som förbrukad. Det förhindrar
att ett block som startat i föregående kvart får en ny starttid.
"""

import datetime
import json
import os

BLOCK_HOURS = 3.0
BLOCK_KW = 5.5
WINDOW_START_HOUR = 22
WINDOW_END_HOUR = 7

STATE_FILE = "ev_block.json"


def _state_path():
    try:
        try:
            from . import paths          # importerad som src.ev_block
        except ImportError:
            import paths                 # importerad med src/ på sys.path
        return paths.data_file(STATE_FILE)
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)


def load_booking():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    try:
        start = datetime.datetime.fromisoformat(data["start"])
        end = datetime.datetime.fromisoformat(data["end"])
    except (KeyError, ValueError):
        return None

    return {
        "start": start,
        "end": end,
        "kw": float(data.get("kw", BLOCK_KW)),
        "order_id": data.get("order_id"),
    }


def save_booking(start, end, kw=BLOCK_KW, order_id=None):
    payload = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "kw": kw,
        "order_id": order_id,
    }
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    return payload


def clear_booking():
    try:
        os.unlink(_state_path())
    except OSError:
        pass


def in_window(ts):
    """Är kvarten inom 22:00–07:00? Sluttiden 07:00 får ingå inte (07:00 tillhör dagen)."""
    return ts.hour >= WINDOW_START_HOUR or ts.hour < WINDOW_END_HOUR


def _quarter_index(ts, now):
    """Antal påbörjade kvartar mellan now och ts (0 om ts <= now)."""
    delta = (ts - now).total_seconds()
    if delta <= 0:
        return 0
    return int(delta // 900)


def quarters_remaining(booking, now):
    """Antal hela kvartar kvar av blocket, räknat från och med nuvarande kvart."""
    if booking is None:
        return 0, 0
    start, end = booking["start"], booking["end"]
    if now >= end:
        return 0, 0          # blocket är förbrukat
    if now <= start:
        return int(round((end - start).total_seconds() // 900)), 0
    # mitt i blocket: räkna bara det som återstår
    remaining = int((end - now).total_seconds() // 900)
    elapsed = int(round((end - start).total_seconds() // 900)) - remaining
    return remaining, elapsed


def is_valid(booking, now, order_active):
    """Ska den sparade bokningen återanvändas?"""
    if not order_active:
        return False
    if booking is None:
        return False
    if now >= booking["end"]:
        return False
    return True


def find_best_block(timestamps, p_in, now, hours=BLOCK_HOURS, kw=BLOCK_KW):
    """
    Välj billigaste sammanhängande fönster mellan 22:00 och 07:00.

    Kvartar som redan påbörjats (ts < now) är inte valbara som start — annars kan
    ett block som startat i föregående kvart få en ny starttid.
    """
    needed = int(round(hours * 4))
    T = len(timestamps)
    if T < needed:
        return None

    eligible = []
    for t in range(T - needed + 1):
        window = timestamps[t:t + needed]
        if not all(in_window(ts) for ts in window):
            continue
        # starten måste ligga i framtiden eller i nuvarande kvart
        if window[0] < now:
            continue
        eligible.append(t)

    if not eligible:
        return None

    best = min(eligible, key=lambda s: sum(p_in[s + k] for k in range(needed)))
    start = timestamps[best]
    end = start + datetime.timedelta(hours=hours)
    return {"start": start, "end": end, "kw": kw}


def resolve_booking(timestamps, p_in, now, order_active, order_id=None):
    """
    Returnerar (booking, is_new) eller (None, False).

    bookingsens start/end används sedan för att bygga ev_grid_kw — behovet
    krymper men fönstret flyttar sig inte.
    """
    current = load_booking()

    if is_valid(current, now, order_active):
        return current, False

    if not order_active:
        # Ingen order: glöm gammal bokning så nästa order får ett färskt block.
        if current is not None:
            clear_booking()
        return None, False

    # tvinga nytt block om ordern gjorts om
    if current is not None and order_id is not None and current.get("order_id") != order_id:
        pass  # faller igenom till nytt val

    chosen = find_best_block(timestamps, p_in, now)
    if chosen is None:
        return None, False

    save_booking(chosen["start"], chosen["end"], chosen["kw"], order_id)
    return chosen, True


def build_ev_array(timestamps, booking, now):
    """
    Bygg ev_grid_kw för horisonten från den fasta bokningen.

    Endast kvartar som ligger inom [max(start, nuvarande kvart), end) får effekt.
    Passerade kvartar får 0 — de är redan laddade.
    """
    import numpy as np

    ev = np.zeros(len(timestamps))
    if booking is None:
        return ev

    start, end = booking["start"], booking["end"]
    kw = booking.get("kw", BLOCK_KW)

    # "nu" avrundas NEDÅT till kvartens början. Tidigare jämfördes mot exakt
    # nuvarande tidpunkt, vilket gjorde att den innevarande kvarten aldrig fick
    # effekt: klockan 04:11 är timestamps[0] = 04:00 och 04:00 >= 04:11 är falskt.
    # Följden var att ev_charge_now alltid blev False och laddboxen stängdes av
    # var 15:e sekund utan att bilen någonsin laddade. Kvartar som redan passerat
    # får fortfarande 0.
    try:
        now_q = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    except (AttributeError, ValueError):
        now_q = now

    for t, ts in enumerate(timestamps):
        if start <= ts < end and ts >= now_q:
            ev[t] = kw
    return ev
