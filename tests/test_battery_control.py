"""Tester for src/battery_control.py - ett testfall per styrmode (spec 7 a-e).

Kor:   python3 -m unittest discover -s tests -v
eller: python3 tests/test_battery_control.py

Testerna ror INGEN vaxelriktare. `build_actions()` ar en ren funktion, sa hela
styrlogiken kan verifieras utan att skriva ett enda register.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import battery_control as bc  # noqa: E402


# Fixerad tid mitt i en kvart, sa fonsterberakningen ar reproducerbar.
NOW = datetime.datetime(2026, 11, 23, 12, 30, 0)
VOLTAGE = 400.0


def actions_for(mode, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("voltage", VOLTAGE)
    return bc.build_actions(mode, **kw)


def by_entity(actions):
    """entity_id -> aktion, sa testet kan fraga 'vad skrevs hit?'."""
    return {a["data"]["entity_id"]: a for a in actions}


def value_of(actions, entity):
    a = by_entity(actions).get(entity)
    if a is None:
        return None
    if a["service"] == "turn_on":
        return "on"
    if a["service"] == "turn_off":
        return "off"
    return a["data"].get("value", a["data"].get("time"))


class ModeChoiceTest(unittest.TestCase):
    """choose_mode: tariffonstret avgor HUR, aldrig OM batteriet laddar."""

    def test_charge_wins_over_everything(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=5.0, discharge_kw=0.0, is_tariff_window=True),
            bc.MODE_CHARGE,
        )
        self.assertEqual(
            bc.choose_mode(charge_kw=5.0, discharge_kw=3.0, is_tariff_window=True),
            bc.MODE_CHARGE,
        )

    def test_discharge_uses_grid_power_inside_window(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=0.0, discharge_kw=3.0, is_tariff_window=True),
            bc.MODE_DISCHARGE_GRID,
        )

    def test_discharge_uses_battery_power_outside_window(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=0.0, discharge_kw=3.0, is_tariff_window=False),
            bc.MODE_DISCHARGE_BATTERY,
        )

    def test_idle_inside_window_uses_grid_power(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=0.0, discharge_kw=0.0, is_tariff_window=True),
            bc.MODE_IDLE_GRID,
        )

    def test_idle_outside_window_is_plain_idle(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=0.0, discharge_kw=0.0, is_tariff_window=False),
            bc.MODE_IDLE,
        )

    def test_noise_below_threshold_is_idle(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=0.04, discharge_kw=0.03, is_tariff_window=False),
            bc.MODE_IDLE,
        )

    def test_none_values_are_treated_as_zero(self):
        self.assertEqual(
            bc.choose_mode(charge_kw=None, discharge_kw=None, is_tariff_window=False),
            bc.MODE_IDLE,
        )


class QuarterWindowTest(unittest.TestCase):
    """Spec 7a: start = kvartens start, slut = kvartens slut + marginal.

    Marginalen ar en hel minut, inte 30 s: TOU-slotens register 43708..43714
    har minutupplosning och ingen sekund. Se END_MARGIN_MINUTES.
    """

    def test_window_at_quarter_start(self):
        start, end = bc.quarter_window(datetime.datetime(2026, 11, 23, 12, 15, 0))
        self.assertEqual(start, "12:15:00")
        self.assertEqual(end, "12:31:00")

    def test_window_mid_quarter(self):
        start, end = bc.quarter_window(datetime.datetime(2026, 11, 23, 12, 22, 47))
        self.assertEqual(start, "12:15:00")
        self.assertEqual(end, "12:31:00")

    def test_window_crosses_hour(self):
        start, end = bc.quarter_window(datetime.datetime(2026, 11, 23, 12, 59, 30))
        self.assertEqual(start, "12:45:00")
        self.assertEqual(end, "13:01:00")

    def test_window_crosses_midnight(self):
        """Sista kvarten far INTE vicka over midnatt (start > slut = odefinierat)."""
        start, end = bc.quarter_window(datetime.datetime(2026, 11, 23, 23, 50, 0))
        self.assertEqual(start, "23:45:00")
        self.assertEqual(end, "23:59:00")


class CurrentConversionTest(unittest.TestCase):
    """Laddeffekt kW -> ampere, med BMS-taket 50 A."""

    def test_zero_kw_is_zero_amps(self):
        self.assertEqual(bc.charge_current_a(0.0, 400.0), 0.0)

    def test_ten_kw_at_400v_is_25a(self):
        self.assertEqual(bc.charge_current_a(10.0, 400.0), 25.0)

    def test_clamped_at_bms_limit(self):
        # 30 kW vid 400 V = 75 A -> ska klamas till 50 A.
        self.assertEqual(bc.charge_current_a(30.0, 400.0), 50.0)

    def test_bad_voltage_falls_back_to_360v(self):
        # 10 kW / 360 V = 27.8 A
        self.assertEqual(bc.charge_current_a(10.0, 0.0), 27.8)
        self.assertEqual(bc.charge_current_a(10.0, None), 27.8)


class ModeChargeTest(unittest.TestCase):
    """(a) Uppladdning - spec 7a."""

    def setUp(self):
        self.a = actions_for(bc.MODE_CHARGE, charge_kw=8.0)

    def test_charge_limit_power_is_10000(self):
        # Detta ar felet som stal natten 23 nov: vardet 0 blockerade allt.
        self.assertEqual(value_of(self.a, bc.ENTITY["charge_limit_power"]), 10000)

    def test_work_mode_is_self_use(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["self_use"]), "on")

    def test_peak_shaving_is_off(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_shaving"]), "off")

    def test_grid_charge_allowed(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["allow_grid_charge"]), "on")

    def test_max_soc_is_100(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["max_charge_soc"]), 100)

    def test_cutoff_soc_is_100(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_cutoff_soc"]), 100)

    def test_current_matches_8kw_at_400v(self):
        # 8000 W / 400 V = 20.0 A
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_current"]), 20.0)

    def test_window_is_quarter_start_to_end_plus_margin(self):
        # Slutet ar kvartens slut + 1 min (minutupplosning i slot-registren).
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_start"]), "12:30:00")
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_end"]), "12:46:00")

    def test_slot_5_switch_is_last_and_on(self):
        # Grinden slas pa SIST - allt annat maste vara skrivet forst.
        self.assertEqual(self.a[-1]["data"]["entity_id"], bc.ENTITY["tou5_switch"])
        self.assertEqual(self.a[-1]["service"], "turn_on")

    def test_other_tou_slots_are_closed(self):
        # Tva samtidiga TOU-fonster ger odefinierad styrning.
        for slot, ent in bc.OTHER_TOU_CHARGE_SLOTS.items():
            self.assertEqual(value_of(self.a, ent), "off", f"slot {slot} lamnades")

    def test_forcecharge_peakshaving_off(self):
        # Bit 8 strypte TOU-laddningen till 0 W 2026-11-23.
        self.assertEqual(value_of(self.a, bc.ENTITY["forcecharge_peakshav"]), "off")

    def test_backup_soc_at_safe_floor(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["backup_soc"]), 15)

    def test_does_not_touch_discharge_limit(self):
        self.assertIsNone(value_of(self.a, bc.ENTITY["discharge_limit_power"]))

    def test_prerequisites_precede_window(self):
        ids = [x["data"]["entity_id"] for x in self.a]
        gate = ids.index(bc.ENTITY["tou5_switch"])
        for key in ("charge_limit_power", "self_use", "allow_grid_charge", "peak_shaving"):
            self.assertLess(
                ids.index(bc.ENTITY[key]), gate,
                f"{key} maste skrivas fore TOU-grinden",
            )


class ModeDischargeBatteryTest(unittest.TestCase):
    """(b) Urladdning styrd med batterieffekt - spec 7b."""

    def setUp(self):
        self.a = actions_for(bc.MODE_DISCHARGE_BATTERY, discharge_kw=4.0)

    def test_work_mode_is_self_use(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["self_use"]), "on")

    def test_peak_shaving_off(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_shaving"]), "off")

    def test_discharge_limit_is_4000w(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["discharge_limit_power"]), 4000)

    def test_backup_soc_is_15(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["backup_soc"]), 15)

    def test_tou_window_is_closed_before_mode_writes(self):
        ids = [x["data"]["entity_id"] for x in self.a]
        close = ids.index(bc.ENTITY["tou5_switch"])
        self.assertEqual(self.a[close]["service"], "turn_off")
        # Stangningen ska ske fore de lagespecifika skrivningarna.
        for key in ("discharge_limit_power", "self_use"):
            self.assertLess(close, ids.index(bc.ENTITY[key]))

    def test_grid_charge_blocked(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["allow_grid_charge"]), "off")

    def test_does_not_touch_peak_registers(self):
        self.assertIsNone(value_of(self.a, bc.ENTITY["peak_max_grid"]))
        self.assertIsNone(value_of(self.a, bc.ENTITY["peak_baseline_soc"]))

    def test_fractional_kw_rounds_to_watts(self):
        a = actions_for(bc.MODE_DISCHARGE_BATTERY, discharge_kw=2.345)
        self.assertEqual(value_of(a, bc.ENTITY["discharge_limit_power"]), 2345)


class ModeDischargeGridTest(unittest.TestCase):
    """(c) Urladdning styrd med nateffekt - spec 7c."""

    def setUp(self):
        self.a = actions_for(
            bc.MODE_DISCHARGE_GRID, discharge_kw=4.0, grid_import_kw=2.0
        )

    def test_work_mode_is_peak_shaving(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_shaving"]), "on")

    def test_peak_max_grid_is_2000w(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_max_grid"]), 2000)

    def test_peak_baseline_soc_is_100(self):
        # Maste vara >= SOC, annars tar invertern allt fran batteriet.
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_baseline_soc"]), 100)

    def test_discharge_limit_allows_discharge(self):
        # Peak shaving kan inte halla taket om batteriet inte far ladda ur.
        self.assertEqual(value_of(self.a, bc.ENTITY["discharge_limit_power"]), 10000)

    def test_tou_window_closed(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_switch"]), "off")

    def test_grid_cap_is_clamped_to_register_max(self):
        a = actions_for(bc.MODE_DISCHARGE_GRID, discharge_kw=4.0, grid_import_kw=99.0)
        self.assertEqual(value_of(a, bc.ENTITY["peak_max_grid"]), bc.PEAK_GRID_MAX_W)

    def test_negative_grid_import_is_clamped_to_zero(self):
        a = actions_for(bc.MODE_DISCHARGE_GRID, discharge_kw=4.0, grid_import_kw=-3.0)
        self.assertEqual(value_of(a, bc.ENTITY["peak_max_grid"]), 0)

    def test_self_use_switch_not_forced_on(self):
        # Peak shaving ar ett eget baslage; vi ror inte self-use-switchen.
        self.assertIsNone(value_of(self.a, bc.ENTITY["self_use"]))


class ModeIdleTest(unittest.TestCase):
    """(d) Inaktivt batteri - spec 7d."""

    def setUp(self):
        self.a = actions_for(bc.MODE_IDLE)

    def test_work_mode_is_self_use(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["self_use"]), "on")

    def test_discharge_limit_is_idle_floor(self):
        # 0 ar ogiltigt i enheten och ger FRI urladdning (matt 2026-11-25:
        # 3 064 W vid 43131 = 0). 200 W ar lagsta grans som bevisligen biter.
        self.assertEqual(
            value_of(self.a, bc.ENTITY["discharge_limit_power"]),
            bc.DISCHARGE_LIMIT_IDLE_W,
        )
        self.assertNotEqual(bc.DISCHARGE_LIMIT_IDLE_W, 0)

    def test_peak_shaving_off(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_shaving"]), "off")

    def test_backup_soc_is_15(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["backup_soc"]), 15)

    def test_tou_window_closed(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_switch"]), "off")

    def test_grid_charge_blocked(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["allow_grid_charge"]), "off")


class ModeIdleGridTest(unittest.TestCase):
    """(e) Inaktivt batteri styrt med nateffekten - spec 7e."""

    def setUp(self):
        self.a = actions_for(bc.MODE_IDLE_GRID)

    def test_work_mode_is_peak_shaving(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_shaving"]), "on")

    def test_peak_max_grid_is_14kw(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_max_grid"]), 14000)

    def test_peak_baseline_soc_is_100(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["peak_baseline_soc"]), 100)

    def test_discharge_limit_is_idle_floor(self):
        # 0 ar ogiltigt i enheten och ger FRI urladdning (matt 2026-11-25:
        # 3 064 W vid 43131 = 0). 200 W ar lagsta grans som bevisligen biter.
        self.assertEqual(
            value_of(self.a, bc.ENTITY["discharge_limit_power"]),
            bc.DISCHARGE_LIMIT_IDLE_W,
        )
        self.assertNotEqual(bc.DISCHARGE_LIMIT_IDLE_W, 0)

    def test_tou_window_closed(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["tou5_switch"]), "off")

    def test_grid_charge_blocked(self):
        self.assertEqual(value_of(self.a, bc.ENTITY["allow_grid_charge"]), "off")


class InvariantTest(unittest.TestCase):
    """Gemensamma regler som maste halla i ALLA lagen."""

    def _all(self, mode):
        return actions_for(mode, charge_kw=1.0, discharge_kw=1.0, grid_import_kw=1.0)

    def test_every_mode_turns_forcecharge_peakshaving_off(self):
        for mode in bc.ALL_MODES:
            self.assertEqual(
                value_of(self._all(mode), bc.ENTITY["forcecharge_peakshav"]),
                "off", f"{mode} lamnade bit 8 pa",
            )

    def test_every_mode_sets_backup_soc_floor(self):
        for mode in bc.ALL_MODES:
            self.assertEqual(
                value_of(self._all(mode), bc.ENTITY["backup_soc"]),
                15, f"{mode} satte inte reservgolvet",
            )

    def test_only_charge_mode_touches_charge_limit_power(self):
        # I andra lagen lamnas vardet - att sanka det vore att gora laddning
        # omojlig vid nasta laddkvart.
        for mode in bc.ALL_MODES:
            v = value_of(self._all(mode), bc.ENTITY["charge_limit_power"])
            if mode == bc.MODE_CHARGE:
                self.assertEqual(v, 10000)
            else:
                self.assertIsNone(v, f"{mode} rorde charge_limit_power")

    def test_only_charge_mode_opens_tou_window(self):
        for mode in bc.ALL_MODES:
            state = value_of(self._all(mode), bc.ENTITY["tou5_switch"])
            if mode == bc.MODE_CHARGE:
                self.assertEqual(state, "on")
            else:
                self.assertEqual(state, "off", f"{mode} lamnade TOU-fonstret oppet")

    def test_only_charge_mode_allows_grid_charge(self):
        for mode in bc.ALL_MODES:
            state = value_of(self._all(mode), bc.ENTITY["allow_grid_charge"])
            if mode == bc.MODE_CHARGE:
                self.assertEqual(state, "on")
            else:
                self.assertEqual(state, "off", f"{mode} tillat natladdning")

    def test_grid_peak_shaving_follows_peak_cap_modes(self):
        """Importtaket (43483 bit 7) maste folja de lagen som anvander taket.

        Bevis 2026-11-25: med bit 7 AV ar 43488 verkningslost (69,8 % av
        matpunkterna over ett 6 kW-tak). Med bit 7 PA holls taket. Koden satte
        tidigare bara bit 11 + 43488, dvs. skrev ett tak som inte gallde.
        """
        caps_on = {bc.MODE_DISCHARGE_GRID, bc.MODE_IDLE_GRID}
        for mode in bc.ALL_MODES:
            state = value_of(self._all(mode), bc.ENTITY["grid_peak_shaving"])
            expected = "on" if mode in caps_on else "off"
            self.assertEqual(
                state, expected,
                f"{mode}: importtaket skulle vara {expected}, var {state}",
            )

    def test_charge_mode_never_leaves_import_cap_active(self):
        """Ett kvarlamnat tak kapar laddningen till (tak - huslast).

        Matt 2026-11-24 22:00-23:00: tak 5000 W, huslast 2571 W, plan 4000 W
        -> laddningen korde 1970 W i stallet for 4000 W.
        """
        acts = self._all(bc.MODE_CHARGE)
        self.assertEqual(value_of(acts, bc.ENTITY["grid_peak_shaving"]), "off")
        # ...och taket ska inte ens skrivas i laddlage - det ar inte vara att
        # rora ett varde som strax ska vara avstangt.
        self.assertIsNone(value_of(acts, bc.ENTITY["peak_max_grid"]))

    def test_never_writes_raw_modbus_registers(self):
        for mode in bc.ALL_MODES:
            for x in self._all(mode):
                self.assertIn(
                    x["domain"], ("number", "switch", "time", "select"),
                    "endast HA-entiteter far skrivas",
                )

    def test_every_action_has_entity_and_reason(self):
        for mode in bc.ALL_MODES:
            for x in self._all(mode):
                self.assertTrue(x["data"].get("entity_id"), f"{mode}: aktion utan entitet")
                self.assertTrue(x.get("why"), f"{mode}: aktion utan motivering")

    def test_no_duplicate_writes_to_same_entity(self):
        # Tva skrivningar till samma entitet i samma lage = sista vinner,
        # och da ar den forsta en bugg (t.ex. sa pa och sedan av).
        for mode in bc.ALL_MODES:
            ids = [x["data"]["entity_id"] for x in self._all(mode)]
            self.assertEqual(
                len(ids), len(set(ids)), f"{mode} skriver samma entitet tva ganger"
            )

    def test_every_mode_closes_all_other_tou_slots(self):
        for mode in bc.ALL_MODES:
            a = self._all(mode)
            for slot, ent in bc.OTHER_TOU_CHARGE_SLOTS.items():
                self.assertEqual(
                    value_of(a, ent), "off",
                    f"{mode} lamnade TOU slot {slot} pa",
                )

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            bc.build_actions("nonsense")

    def test_all_five_modes_documented(self):
        self.assertEqual(len(bc.ALL_MODES), 5)
        for mode in bc.ALL_MODES:
            self.assertNotIn("Okant", bc.describe(mode))


class ExecuteTest(unittest.TestCase):
    """execute(): verkstallande, dry run och feltolerans."""

    class FakeClient:
        def __init__(self, fail_on=None):
            self.calls = []
            self.fail_on = fail_on or set()

        def call_service(self, domain, service, data):
            ent = data.get("entity_id", "")
            if ent in self.fail_on:
                raise RuntimeError("simulerat fel")
            self.calls.append(
                (domain, service, data.get("value", data.get("time")))
            )

    def test_dry_run_writes_nothing(self):
        c = self.FakeClient()
        bc.execute(actions_for(bc.MODE_CHARGE, charge_kw=8.0), c,
                   dry_run=True, logger=lambda *_: None)
        self.assertEqual(c.calls, [])

    def test_live_executes_all(self):
        c = self.FakeClient()
        acts = actions_for(bc.MODE_CHARGE, charge_kw=8.0)
        done = bc.execute(acts, c, dry_run=False, logger=lambda *_: None)
        self.assertEqual(len(done), len(acts))
        self.assertEqual(len(c.calls), len(acts))

    def test_one_failure_does_not_stop_the_rest(self):
        acts = actions_for(bc.MODE_CHARGE, charge_kw=8.0)
        first = acts[0]["data"]["entity_id"]
        c = self.FakeClient(fail_on={first})
        done = bc.execute(acts, c, dry_run=False, logger=lambda *_: None)
        self.assertEqual(len(done), len(acts) - 1)
        # Grinden sists ska fortfarande ha skrivits.
        self.assertIn(
            bc.ENTITY["tou5_switch"],
            [d["data"]["entity_id"] for d in done],
        )

    def test_execution_order_preserved(self):
        c = self.FakeClient()
        acts = actions_for(bc.MODE_CHARGE, charge_kw=8.0)
        bc.execute(acts, c, dry_run=False, logger=lambda *_: None)
        expected = [a["data"].get("value", a["data"].get("time")) for a in acts]
        self.assertEqual([x[2] for x in c.calls], expected)


class DiffTest(unittest.TestCase):
    """diff_actions(): skriv bara det som faktiskt andras."""

    class StateClient:
        def __init__(self, states=None, unreadable=None):
            self.states = dict(states or {})
            self.unreadable = set(unreadable or [])
            self.calls = []

        def get_state(self, entity_id):
            if entity_id in self.unreadable:
                raise RuntimeError("nås inte")
            if entity_id not in self.states:
                return None
            return {"entity_id": entity_id, "state": self.states[entity_id]}

        def call_service(self, domain, service, data):
            self.calls.append((domain, service, data))

    def test_no_change_gives_no_writes(self):
        """Ett lage som redan ar helt satt ska ge noll skrivningar - utom grinden."""
        acts = actions_for(bc.MODE_DISCHARGE_BATTERY, discharge_kw=2.0)
        states = {}
        for a in acts:
            ent = a["data"]["entity_id"]
            if a["service"] == "turn_on":
                states[ent] = "on"
            elif a["service"] == "turn_off":
                states[ent] = "off"
            else:
                states[ent] = a["data"].get("value", a["data"].get("time"))
        c = self.StateClient(states)
        left = bc.diff_actions(acts, c, logger=lambda *_: None)
        self.assertEqual(left, [])

    def test_only_the_diff_is_kept(self):
        acts = actions_for(bc.MODE_DISCHARGE_BATTERY, discharge_kw=2.0)
        target = bc.ENTITY["discharge_limit_power"]
        states = {}
        for a in acts:
            ent = a["data"]["entity_id"]
            if a["service"] == "turn_on":
                states[ent] = "on"
            elif a["service"] == "turn_off":
                states[ent] = "off"
            else:
                states[ent] = a["data"].get("value", a["data"].get("time"))
        states[target] = 5000  # fel varde -> maste skrivas
        c = self.StateClient(states)
        left = bc.diff_actions(acts, c, logger=lambda *_: None)
        self.assertEqual([a["data"]["entity_id"] for a in left], [target])

    def test_unknown_state_is_written(self):
        """Okant/unavailable raknas som 'inte lika'."""
        acts = actions_for(bc.MODE_IDLE)
        c = self.StateClient({}, unreadable=set())
        left = bc.diff_actions(acts, c, logger=lambda *_: None)
        self.assertEqual(len(left), len(acts))

    def test_unreadable_entity_is_written(self):
        """En lasning som kastar far inte tysta styrningen."""
        acts = actions_for(bc.MODE_IDLE)
        ent = bc.ENTITY["discharge_limit_power"]
        c = self.StateClient({}, unreadable={ent})
        left = bc.diff_actions(acts, c, logger=lambda *_: None)
        self.assertIn(ent, [a["data"]["entity_id"] for a in left])

    def test_gate_always_written_when_turning_on(self):
        """tou5_switch skrivs alltid nar den ska sla pa - aven om HA sager 'on'.

        Grinden ar bekraftelsen att forutsattningarna ovan ar skrivna. Hoppar
        vi over den kan ett fonster som enheten sjalv stangt (t.ex. vid en
        omstart av invertern) lamnas stangt hela kvarten.
        """
        acts = actions_for(bc.MODE_CHARGE, charge_kw=4.0)
        gate = bc.ENTITY["tou5_switch"]
        states = {}
        for a in acts:
            ent = a["data"]["entity_id"]
            if a["service"] == "turn_on":
                states[ent] = "on"
            elif a["service"] == "turn_off":
                states[ent] = "off"
            else:
                states[ent] = a["data"].get("value", a["data"].get("time"))
        c = self.StateClient(states)  # grinden sager redan 'on'
        left = bc.diff_actions(acts, c, logger=lambda *_: None)
        self.assertIn(gate, [a["data"]["entity_id"] for a in left])

    def test_order_preserved_after_diff(self):
        acts = actions_for(bc.MODE_CHARGE, charge_kw=4.0)
        c = self.StateClient({})
        left = bc.diff_actions(acts, c, logger=lambda *_: None)
        self.assertEqual(left, acts)

    def test_on_off_normalisation(self):
        """'on'/'off' i HA jamfors som bool, inte som strang."""
        acts = [bc._set_switch("self_use", True, "x")]
        c = self.StateClient({bc.ENTITY["self_use"]: "on"})
        self.assertEqual(bc.diff_actions(acts, c, logger=lambda *_: None), [])

    def test_time_normalisation(self):
        """HA kan svara '23:15' dar vi skrev '23:15:00'."""
        acts = [bc._set_time("tou5_start", "23:15:00", "x")]
        c = self.StateClient({bc.ENTITY["tou5_start"]: "23:15"})
        self.assertEqual(bc.diff_actions(acts, c, logger=lambda *_: None), [])

    def test_float_normalisation(self):
        """15.5 A och 15.50 A ar samma varde."""
        acts = [bc._set_number("tou5_current", 15.5, "x")]
        c = self.StateClient({bc.ENTITY["tou5_current"]: "15.50"})
        self.assertEqual(bc.diff_actions(acts, c, logger=lambda *_: None), [])


class WindowMarginTest(unittest.TestCase):
    """Slutet ligger en hel minut efter kvarten - enheten har minutupplosning."""

    def test_end_is_one_minute_after_quarter(self):
        now = datetime.datetime(2026, 11, 24, 12, 30, 5)
        start, end = bc.quarter_window(now)
        self.assertEqual(start, "12:30:00")
        self.assertEqual(end, "12:46:00")

    def test_seconds_are_always_zero(self):
        """Aldrig :30 - enheten har ingen sekund, och trunkering/avrundning
        av integrationen ar okaend."""
        for minute in (0, 7, 14, 15, 29, 45, 59):
            now = datetime.datetime(2026, 11, 24, 12, minute, 37)
            start, end = bc.quarter_window(now)
            self.assertTrue(start.endswith(":00"), start)
            self.assertTrue(end.endswith(":00"), end)

    def test_last_quarter_does_not_wrap_past_midnight(self):
        """23:45 kvarten: 23:59, inte 00:01 (start > slut ar odefinierat)."""
        now = datetime.datetime(2026, 11, 24, 23, 50, 0)
        start, end = bc.quarter_window(now)
        self.assertEqual(start, "23:45:00")
        self.assertEqual(end, "23:59:00")
        self.assertLess(start, end)

    def test_no_quarter_ever_wraps(self):
        for hour in range(24):
            for minute in (0, 15, 30, 45):
                now = datetime.datetime(2026, 11, 24, hour, minute, 0)
                start, end = bc.quarter_window(now)
                self.assertLess(start, end, f"{hour:02d}:{minute:02d}")


class VerifyWindowTest(unittest.TestCase):
    """verify_charge_window(): laser tillbaka slot 5 och jamfor."""

    class RegClient:
        def __init__(self, slot5=None, mask=None, fail=False, hybrid=0):
            self.slot5 = slot5
            self.mask = mask
            self.fail = fail
            self.hybrid = hybrid  # 43483, bit 7 = importtaket
            self.reads = []

        def call_service(self, domain, service, data, return_response=False):
            self.reads.append((service, data.get("address")))
            if self.fail:
                raise RuntimeError("modbus svarar inte")
            if data.get("address") == bc.TOU_SLOT5_FIRST_REG:
                return {"response": {"values": self.slot5}}
            if data.get("address") == bc.TOU_BITMASK_REG:
                return {"response": {"values": [self.mask]}}
            if data.get("address") == bc.GRID_PEAK_SH_CTRL_REG:
                return {"response": {"values": [self.hybrid]}}
            return None

    def acts(self, charge_kw=4.0):
        now = datetime.datetime(2026, 11, 24, 12, 30, 0)
        return bc.build_actions(bc.MODE_CHARGE, charge_kw=charge_kw,
                                voltage=258.0, now=now)

    def test_matching_registers_pass(self):
        c = self.RegClient(slot5=[100, 155, 0, 12, 30, 12, 46], mask=1 << 4)
        self.assertTrue(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_wrong_current_is_reported(self):
        c = self.RegClient(slot5=[100, 99, 0, 12, 30, 12, 46], mask=1 << 4)
        self.assertFalse(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_wrong_end_is_reported(self):
        """Det har ar hela poangen: enheten har fatt ett annat fonster an vi skrev."""
        c = self.RegClient(slot5=[100, 155, 0, 12, 30, 12, 31], mask=1 << 4)
        self.assertFalse(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_missing_period_bit_is_reported(self):
        """Alla slot-varden ratt men perioden inte aktiv - fonstret ar inte oppet."""
        c = self.RegClient(slot5=[100, 155, 0, 12, 30, 12, 46], mask=0)
        self.assertFalse(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_unreadable_does_not_raise(self):
        c = self.RegClient(fail=True)
        self.assertFalse(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_garbage_response_does_not_raise(self):
        c = self.RegClient(slot5=None, mask=16)
        self.assertFalse(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_active_import_cap_is_reported(self):
        """Ett kvarlamnat importtak kapar laddningen till (tak - huslast).

        Matt 2026-11-24: tak 5000 W, huslast 2571 W -> 1970 W laddning i
        stallet for 4000 W. Ingen skrivning ser fel ut; bara lasningen av
        43483 avslojar det.
        """
        c = self.RegClient(slot5=[100, 155, 0, 12, 30, 12, 46], mask=1 << 4,
                           hybrid=1 << 7)
        self.assertFalse(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_other_hybrid_bits_do_not_trip_the_cap_check(self):
        """43483 ar ett flaggregister - bara bit 7 ar importtaket."""
        c = self.RegClient(slot5=[100, 155, 0, 12, 30, 12, 46], mask=1 << 4,
                           hybrid=1946 & ~(1 << 7))
        self.assertTrue(bc.verify_charge_window(c, self.acts(), logger=lambda *_: None))

    def test_non_charge_mode_is_not_verified(self):
        """Utan laddfonster finns inget att verifiera - och inga registerläsningar."""
        now = datetime.datetime(2026, 11, 24, 12, 30, 0)
        acts = bc.build_actions(bc.MODE_IDLE, now=now)
        c = self.RegClient(slot5=[0], mask=0)
        self.assertTrue(bc.verify_charge_window(c, acts, logger=lambda *_: None))
        self.assertEqual(c.reads, [])

    def test_expected_slot5_extracts_values(self):
        want = bc.expected_slot5(self.acts(charge_kw=8.0))
        self.assertEqual(want["tou5_cutoff_soc"], 100)
        self.assertEqual(want["tou5_start"], "12:30:00")
        self.assertEqual(want["tou5_end"], "12:46:00")
        self.assertAlmostEqual(want["tou5_current"], 31.0, delta=0.2)


class RegisterValuesTest(unittest.TestCase):
    """_register_values(): svaret har olika form beroende pa transport."""

    def test_rest_api_form(self):
        self.assertEqual(
            bc._register_values({"service_response": {"values": [1, 2, 3]}}),
            [1, 2, 3],
        )

    def test_json_string_form(self):
        """curl-vagen ger en JSON-strang av samma sak - inte en dict."""
        payload = ('{"changed_states":[],"service_response":'
                   '{"address":43736,"count":7,"register_type":"holding",'
                   '"values":[100,312,0,10,45,12,0]}}')
        self.assertEqual(
            bc._register_values(payload), [100, 312, 0, 10, 45, 12, 0]
        )

    def test_websocket_form(self):
        self.assertEqual(bc._register_values({"response": {"values": [7]}}), [7])

    def test_plain_list(self):
        self.assertEqual(bc._register_values([5, 6]), [5, 6])

    def test_list_of_dicts_is_not_values(self):
        self.assertIsNone(bc._register_values([{"a": 1}]))

    def test_garbage_returns_none(self):
        for bad in (None, "inte json", {}, {"response": None}, 42):
            self.assertIsNone(bc._register_values(bad))


class VerifyStringResponseTest(unittest.TestCase):
    """Verifieringen maste klara JSON-strang-svaret fran REST-vagen."""

    class StrClient:
        def __init__(self, slot5, mask):
            self.slot5 = slot5
            self.mask = mask

        def call_service(self, domain, service, data, return_response=False):
            if data.get("address") == bc.TOU_SLOT5_FIRST_REG:
                return ('{"service_response":{"values":%s}}' % self.slot5)
            return ('{"service_response":{"values":[%d]}}' % self.mask)

    def test_string_response_passes_on_match(self):
        now = datetime.datetime(2026, 11, 24, 12, 30, 0)
        acts = bc.build_actions(bc.MODE_CHARGE, charge_kw=4.0,
                                voltage=258.0, now=now)
        c = self.StrClient("[100,155,0,12,30,12,46]", 1 << 4)
        self.assertTrue(bc.verify_charge_window(c, acts, logger=lambda *_: None))

    def test_string_response_fails_on_mismatch(self):
        now = datetime.datetime(2026, 11, 24, 12, 30, 0)
        acts = bc.build_actions(bc.MODE_CHARGE, charge_kw=4.0,
                                voltage=258.0, now=now)
        c = self.StrClient("[100,312,0,10,45,12,0]", 0)
        self.assertFalse(bc.verify_charge_window(c, acts, logger=lambda *_: None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
