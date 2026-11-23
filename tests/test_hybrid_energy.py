from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from utsm_telemetry.hybrid_energy import field_data, load_run, physics, score


def test_interval_units_and_reset(tmp_path):
    path = tmp_path / "run.csv"
    pd.DataFrame({
        "timestamp_ms": [0, 1000, 2000, 0, 1000],
        "current_mA": [-2000] * 5,
        "voltage_mV": [24000] * 5,
        "wheel_speed_kph": [18, 21.6, 25.2, 18, 21.6],
        "wheel_speed_valid": [1] * 5,
    }).to_csv(path, index=False)
    frame = load_run(path, "Front Campus")
    assert frame.index.tolist() == [1, 2, 4]
    np.testing.assert_allclose(frame.accel_m_s2, [0.5, 0.5, 0.5])
    np.testing.assert_allclose(frame.target_w, 48)
    assert score(frame, frame.target_w)["absolute_error_wh"] == 0


def test_field_split_and_physics():
    frame, _ = field_data(Path(__file__).resolve().parents[1])
    assert frame.groupby("track").holdout.nunique().eq(2).all()
    assert frame.groupby("run").holdout.nunique().eq(1).all()
    flat = frame.head(5)
    assert np.all(physics(flat.assign(grade_pct=3)) > physics(flat))
