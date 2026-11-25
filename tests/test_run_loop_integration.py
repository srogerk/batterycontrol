"""Integrationstest: plan -> modeval -> entitetsskrivningar, utan vaxelriktare.

Kor: python3 -m unittest discover -s tests -v

Testet efterliknar det run_loop.main() gor efter optimeringen: las planens
kvart, valj lage, bygg aktioner, verkstall mot en falsk klient. Det bevisar att
kopplingen fungerar och att ratt lage valjs for ratt kvart - inte att HA svarar.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import battery_control as bc  # noqa: E402


class FakeClient:
    """Minimal HA-klient. Registrerar anrop, svarar pa tillstand."""

    def __init__(self, states=None):
        self.calls = []
        self.states = states or {}

    def call_service(self, domain, service, data):
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "entity_id": data.get("entity_id"),
                "value": data.get("value", data.get("time")),
            }
        )

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def written(self, entity_id):
        """Sista vardet som skrevs till entiteten, eller None."""
        hits = [c for c in self.calls if c["entity_id"] == entity_id]
        return hits[-1]["value"] if hits else None


def run_cycle(*, charge_kw=0.0, discharge_kw=0.0, grid_import_kw=0.0,
              is_tariff_window=False, now=None, voltage=400.0, dry_run=False):
    """En kvarts styrning, exakt som run_loop gor den."""
    now = now or datetime.datetime(2026, 11, 23, 12, 30, 0)
    mode = bc.choose_mode(
        charge_kw=charge_kw,
        discharge_kw=discharge_kw,
        is_tariff_window=is_tariff_window,
    )
    actions = bc.build_actions(
        mode,
        charge_kw=charge_kw,
        discharge_kw=discharge_kw,
        grid_import_kw=grid_import_kw,
        voltage=voltage,
        now=now,
    )
    client = FakeClient()
    bc.execute(actions, client, dry_run=dry_run, logger=lambda *_: None)
    return mode, actions, client


class FullCycleTest(unittest.TestCase):
    """En kvart per lage, hela vagen fran plan till skrivning."""

    def test_charge_cycle_lands_every_prerequisite(self):
        mode, actions, c = run_cycle(charge_kw=8.0)
        self.assertEqual(mode, bc.MODE_CHARGE)
        # De fem forutsattningarna ur spec 7a.
        self.assertEqual(c.written(bc.ENTITY["charge_limit_power"]), 10000)
        self.assertEqual(c.written(bc.ENTITY["self_use"]), None)  # turn_on
        self.assertIn(
            "turn_on",
            [x["service"] for x in c.calls if x["entity_id"] == bc.ENTITY["self_use"]],
        )
        self.assertIn(
            "turn_on",
            [x["service"] for x in c.calls
             if x["entity_id"] == bc.ENTITY["allow_grid_charge"]],
        )
        self.assertIn(
            "turn_off",
            [x["service"] for x in c.calls
             if x["entity_id"] == bc.ENTITY["peak_shaving"]],
        )
        self.assertEqual(c.written(bc.ENTITY["max_charge_soc"]), 100)
        # Fonstret
        self.assertEqual(c.written(bc.ENTITY["tou5_current"]), 20.0)
        self.assertEqual(c.written(bc.ENTITY["tou5_start"]), "12:30:00")
        self.assertEqual(c.written(bc.ENTITY["tou5_end"]), "12:46:00")
        self.assertIn(
            "turn_on",
            [x["service"] for x in c.calls
             if x["entity_id"] == bc.ENTITY["tou5_switch"]],
        )

    def test_charge_always_last_action_is_the_gate(self):
        _, actions, c = run_cycle(charge_kw=8.0)
        self.assertEqual(c.calls[-1]["entity_id"], bc.ENTITY["tou5_switch"])
        self.assertEqual(c.calls[-1]["service"], "turn_on")

    def test_tariff_discharge_cycle_sets_grid_cap(self):
        mode, _, c = run_cycle(discharge_kw=4.0, grid_import_kw=2.0,
                               is_tariff_window=True)
        self.assertEqual(mode, bc.MODE_DISCHARGE_GRID)
        self.assertEqual(c.written(bc.ENTITY["peak_max_grid"]), 2000)
        self.assertEqual(c.written(bc.ENTITY["peak_baseline_soc"]), 100)
        self.assertIn(
            "turn_on",
            [x["service"] for x in c.calls
             if x["entity_id"] == bc.ENTITY["peak_shaving"]],
        )

    def test_night_discharge_cycle_sets_battery_cap(self):
        mode, _, c = run_cycle(discharge_kw=4.0, grid_import_kw=0.0,
                               is_tariff_window=False)
        self.assertEqual(mode, bc.MODE_DISCHARGE_BATTERY)
        self.assertEqual(c.written(bc.ENTITY["discharge_limit_power"]), 4000)
        self.assertIn(
            "turn_on",
            [x["service"] for x in c.calls
             if x["entity_id"] == bc.ENTITY["self_use"]],
        )

    def test_tariff_idle_cycle_uses_14kw_cap(self):
        mode, _, c = run_cycle(is_tariff_window=True)
        self.assertEqual(mode, bc.MODE_IDLE_GRID)
        self.assertEqual(c.written(bc.ENTITY["peak_max_grid"]), 14000)
        # 0 ar ogiltigt i enheten -> fri urladdning (matt 3 064 W 2026-11-25).
        self.assertEqual(
            c.written(bc.ENTITY["discharge_limit_power"]), bc.DISCHARGE_LIMIT_IDLE_W
        )

    def test_night_idle_cycle_floors_discharge(self):
        mode, _, c = run_cycle(is_tariff_window=False)
        self.assertEqual(mode, bc.MODE_IDLE)
        # Inte 0: 0 ar ogiltigt i enheten och ger fri urladdning.
        self.assertEqual(
            c.written(bc.ENTITY["discharge_limit_power"]), bc.DISCHARGE_LIMIT_IDLE_W
        )

    def test_dry_run_writes_nothing_at_all(self):
        for charge, discharge, tariff in [
            (8.0, 0.0, True), (0.0, 4.0, True), (0.0, 4.0, False),
            (0.0, 0.0, True), (0.0, 0.0, False),
        ]:
            _, _, c = run_cycle(
                charge_kw=charge, discharge_kw=discharge,
                is_tariff_window=tariff, dry_run=True,
            )
            self.assertEqual(c.calls, [], f"dry run skrev vid {charge}/{discharge}/{tariff}")


class TransitionTest(unittest.TestCase):
    """Att ga mellan lagen far inte lamna nagot halvvags."""

    def _written_map(self, client):
        out = {}
        for c in client.calls:
            if c["service"] in ("turn_on", "turn_off"):
                out[c["entity_id"]] = c["service"] == "turn_on"
            else:
                out[c["entity_id"]] = c["value"]
        return out

    def test_charge_to_idle_closes_window_and_zeroes_current(self):
        _, _, c = run_cycle(is_tariff_window=False)
        w = self._written_map(c)
        self.assertFalse(w[bc.ENTITY["tou5_switch"]])
        self.assertEqual(w[bc.ENTITY["tou5_current"]], 0.0)

    def test_charge_to_idle_blocks_grid_charge(self):
        _, _, c = run_cycle(is_tariff_window=False)
        w = self._written_map(c)
        self.assertFalse(w[bc.ENTITY["allow_grid_charge"]])

    def test_idle_to_charge_reopens_everything(self):
        _, _, c = run_cycle(charge_kw=8.0)
        w = self._written_map(c)
        self.assertTrue(w[bc.ENTITY["tou5_switch"]])
        self.assertTrue(w[bc.ENTITY["allow_grid_charge"]])
        self.assertTrue(w[bc.ENTITY["self_use"]])
        self.assertFalse(w[bc.ENTITY["peak_shaving"]])
        self.assertGreater(w[bc.ENTITY["tou5_current"]], 0)

    def test_switch_between_discharge_modes_flips_peak_shaving(self):
        # Natt -> dag: batterieffekt -> nateffekt
        _, _, night = run_cycle(discharge_kw=3.0, is_tariff_window=False)
        _, _, day = run_cycle(discharge_kw=3.0, grid_import_kw=2.0,
                              is_tariff_window=True)
        self.assertFalse(self._written_map(night)[bc.ENTITY["peak_shaving"]])
        self.assertTrue(self._written_map(day)[bc.ENTITY["peak_shaving"]])

    def test_every_transition_keeps_backup_floor(self):
        seqs = [(8.0, 0.0, True), (0.0, 4.0, True), (0.0, 4.0, False),
                (0.0, 0.0, True), (0.0, 0.0, False)]
        for charge, discharge, tariff in seqs:
            _, _, c = run_cycle(charge_kw=charge, discharge_kw=discharge,
                                is_tariff_window=tariff)
            self.assertEqual(
                self._written_map(c)[bc.ENTITY["backup_soc"]], 15,
                f"reservgolvet tappades vid {charge}/{discharge}/{tariff}",
            )


class SafetyTest(unittest.TestCase):
    """Granser som inte far overskridas, oavsett planens indata."""

    RELAY = 25 * 3 * 233  # 17475 W vid 25 A huvudsakring

    def test_grid_cap_never_exceeds_relay_upstream_of_register(self):
        # Register 43488 har 15 kW tak; sakringen ar 17,5 kW. Bada ar ok,
        # men koden ska aldrig kunna prestera mer an registrets tak.
        for cap in (0, 5, 14, 15, 99):
            _, _, c = run_cycle(discharge_kw=3.0, grid_import_kw=cap,
                                is_tariff_window=True)
            written = c.written(bc.ENTITY["peak_max_grid"])
            self.assertLessEqual(written, bc.PEAK_GRID_MAX_W)
            self.assertLessEqual(written, self.RELAY)

    def test_charge_current_never_exceeds_bms(self):
        for kw in (5, 8, 10, 15, 30, 100):
            _, _, c = run_cycle(charge_kw=kw)
            self.assertLessEqual(c.written(bc.ENTITY["tou5_current"]),
                                 bc.BMS_MAX_CHARGE_A)

    def test_absurd_voltage_does_not_break_charge_current(self):
        for v in (0, 50, 9999, None):
            _, _, c = run_cycle(charge_kw=10.0, voltage=v)
            amps = c.written(bc.ENTITY["tou5_current"])
            self.assertGreater(amps, 0)
            self.assertLessEqual(amps, bc.BMS_MAX_CHARGE_A)

    def test_negative_plan_values_are_treated_as_no_action(self):
        mode, _, c = run_cycle(charge_kw=-5.0, discharge_kw=-3.0,
                               is_tariff_window=False)
        self.assertEqual(mode, bc.MODE_IDLE)
        self.assertEqual(
            c.written(bc.ENTITY["discharge_limit_power"]), bc.DISCHARGE_LIMIT_IDLE_W
        )


class RegressionTest(unittest.TestCase):
    """De tva fel som stal natten 2026-11-23 far inte kunna ateruppsta."""

    def test_charge_limit_power_is_never_zero_in_charge_mode(self):
        # Vardet 0 blockerade ALL natladdning i timmar.
        for kw in (0.1, 1, 5, 8, 10):
            _, _, c = run_cycle(charge_kw=kw)
            v = c.written(bc.ENTITY["charge_limit_power"])
            if v is not None:
                self.assertGreater(v, 0, f"charge_limit_power=0 vid {kw} kW")

    def test_forcecharge_peakshaving_is_off_in_every_mode(self):
        # Bit 8 strypte TOU-laddningen till 0 W trots korrekt konfiguration.
        cases = [(8.0, 0.0, True), (8.0, 0.0, False), (0.0, 4.0, True),
                 (0.0, 4.0, False), (0.0, 0.0, True), (0.0, 0.0, False)]
        for charge, discharge, tariff in cases:
            _, _, c = run_cycle(charge_kw=charge, discharge_kw=discharge,
                                is_tariff_window=tariff)
            self.assertIn(
                "turn_off",
                [x["service"] for x in c.calls
                 if x["entity_id"] == bc.ENTITY["forcecharge_peakshav"]],
                f"bit 8 lamnades pa vid {charge}/{discharge}/{tariff}",
            )

    def test_no_live_mode_ever_leaves_tou_window_open_without_charge(self):
        for charge, discharge, tariff in [(0.0, 4.0, True), (0.0, 4.0, False),
                                          (0.0, 0.0, True), (0.0, 0.0, False)]:
            _, _, c = run_cycle(charge_kw=charge, discharge_kw=discharge,
                                is_tariff_window=tariff)
            tou_writes = [x for x in c.calls
                          if x["entity_id"] == bc.ENTITY["tou5_switch"]]
            self.assertTrue(tou_writes, "TOU-fonstret skrevs aldrig")
            self.assertTrue(
                all(x["service"] == "turn_off" for x in tou_writes),
                f"fonstret lamnades oppet vid {charge}/{discharge}/{tariff}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
