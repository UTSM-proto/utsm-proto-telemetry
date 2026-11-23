"""Train and score a physics-pretrained neural power model, with no dashboard dependency."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from utsm_telemetry.core import haversine_distance, read_gpx
from utsm_telemetry.simulation import _physics_propulsion_power_w, build_motor_config

FEATURES = ["speed_kph", "accel_m_s2"]
CONTEXT = ["grade_pct", "curvature_1_m"]
SEED = 42


def physics(frame):
    motor = build_motor_config()
    return np.array([
        _physics_propulsion_power_w(speed, accel, grade, motor, curvature_1_m=curve)
        for speed, accel, grade, curve in
        frame[FEATURES + CONTEXT].itertuples(index=False, name=None)
    ])


def synthetic(root, count):
    """Sample feasible speed transitions and the repo's reference strategy segments."""
    rng = np.random.default_rng(SEED)
    speed = rng.uniform(2, 45, count)
    accel = np.clip(rng.normal(0, 0.7, count), -2, 2)
    frame = pd.DataFrame(dict(speed_kph=speed, accel_m_s2=accel,
                              grade_pct=0.0, curvature_1_m=0.0))
    for path in sorted((root / "data/tracks").glob("*/*strategy-segments.csv")):
        segments = pd.read_csv(path).rename(columns={
            "target_speed_kph": "speed_kph", "accel_demand_m_s2": "accel_m_s2",
        })
        # Pretrain the speed/acceleration component at neutral track context.
        # Grade and curvature enter through the exact physics adjustment at inference.
        frame = pd.concat([frame, segments[FEATURES].assign(
            grade_pct=0.0, curvature_1_m=0.0)], ignore_index=True)
    return frame


def load_run(path, track, gpx_path=None):
    frame = pd.read_csv(path)
    t = pd.to_numeric(frame.timestamp_ms, errors="coerce") / 1000
    if gpx_path is not None:
        gps = read_gpx(str(gpx_path)).sort_values("time").drop_duplicates("time")
        gps_t = (gps.time - gps.time.iloc[0]).dt.total_seconds().to_numpy()
        lat, lon = gps.lat.to_numpy(), gps.lon.to_numpy()
        step = haversine_distance(lat[:-1], lon[:-1], lat[1:], lon[1:])
        gps_speed = pd.Series(np.r_[np.nan, 3.6 * step / np.diff(gps_t)]).rolling(
            3, min_periods=1).median()
        # Same clock origin as the repository's default GPX alignment.
        speed = pd.Series(np.interp(t, gps_t, gps_speed, left=np.nan, right=np.nan))
    else:
        name = "wheel_speed_kph" if "wheel_speed_kph" in frame else "wheel_speed_kmph"
        speed = pd.to_numeric(frame[name], errors="coerce").where(frame.wheel_speed_valid.eq(1))
        if "gps_speed_kmph" in frame:
            gps = frame.gps_speed_kmph.where(
                frame.gps_location_valid.eq(1) & frame.gps_sats.ge(4) & frame.gps_hdop.le(3))
            speed = speed.mask((speed - gps).abs().gt(10))
            speed = speed.fillna(gps.where(~frame.wheel_speed_valid.eq(1)))

    current = pd.to_numeric(frame.current_mA, errors="coerce")
    voltage = pd.to_numeric(frame.voltage_mV, errors="coerce")
    watts = (current.abs() * voltage / 1e6).where(current.abs().le(60000) & voltage.between(15000, 40000))
    dt = t.diff()
    valid_speed = speed.where(speed.between(0, 60))
    block = (~dt.between(0.001, 2) | valid_speed.isna() | valid_speed.shift().isna()).cumsum()
    smooth = valid_speed.groupby(block).transform(lambda s: s.rolling(3, min_periods=1).median())
    accel = smooth.groupby(block).diff() / (3.6 * dt)
    run = pd.DataFrame(dict(speed_kph=(smooth + smooth.shift()) / 2, accel_m_s2=accel,
                            grade_pct=0.0, curvature_1_m=0.0,
                            target_w=(watts + watts.shift()) / 2, dt_s=dt))
    keep = (run.speed_kph.between(2, 60) & accel.abs().le(4)
            & dt.between(0.001, 2) & run.target_w.notna())
    run = run.loc[keep].copy()
    run["track"] = track
    run["run"] = path.stem
    return run


def field_data(root):
    base = root / "data/runs"
    jobs = []
    for path in sorted((base / "laval").glob("*.csv")):
        jobs.append((path, "Laval", None, path.name.startswith("2026-09-20")))
    for path in sorted(base.glob("front-campus-*/*-telemetry.csv")):
        jobs.append((path, "Front Campus", None, "-run-03-" in path.name))
    for name, test in [("morning-run", False), ("afternoon-run", True)]:
        folder = base / name
        jobs.append((next(folder.glob("*.csv")), "Indianapolis", next(folder.glob("*.gpx")), test))
    frames, sources, digests = [], [], set()
    for path, track, gpx, test in jobs:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in digests:
            raise ValueError(f"Duplicate telemetry capture: {path}")
        digests.add(digest)
        try:
            run = load_run(path, track, gpx)
        except pd.errors.EmptyDataError:
            run = pd.DataFrame(columns=["dt_s"])
        accepted = bool(len(run) >= 50 and run.dt_s.sum() >= 20)
        sources.append(dict(path=path.relative_to(root).as_posix(), sha256=digest,
                            track=track, holdout=test, accepted=accepted,
                            intervals=len(run), seconds=float(run.dt_s.sum()),
                            gpx_sha256=hashlib.sha256(gpx.read_bytes()).hexdigest() if gpx else None))
        if accepted:
            frames.append(run.assign(holdout=test))
    frame = pd.concat(frames, ignore_index=True)
    if frame.groupby("track").holdout.nunique().min() != 2 or frame.track.nunique() != 3:
        raise ValueError("Each track needs accepted development and holdout data")
    return frame, sources


def network(width):
    return make_pipeline(StandardScaler(), MLPRegressor(
        hidden_layer_sizes=(width, width), random_state=SEED, max_iter=220,
        learning_rate_init=0.002, batch_size=256,
    ))


def encode(net, x):
    scaler, mlp = net.steps[0][1], net.steps[1][1]
    h = scaler.transform(x)
    for weights, bias in zip(mlp.coefs_[:-1], mlp.intercepts_[:-1]):
        h = np.maximum(h @ weights + bias, 0)
    return np.column_stack([scaler.transform(x), h])


def predict_power(model, frame):
    """Predict interval or segment battery draw. Optional grade/curvature default to zero."""
    frame = frame.assign(**{name: frame.get(name, 0.0) for name in CONTEXT})
    x = frame[FEATURES]
    neutral = frame.assign(grade_pct=0.0, curvature_1_m=0.0)
    adjustment = physics(frame) - physics(neutral)
    net = model["pretrained"]
    return np.maximum(net.predict(x) + model["correction"].predict(encode(net, x)) + adjustment, 0)


def weights(frame):
    return frame.dt_s / frame.groupby("run").dt_s.transform("sum")


def fit_correction(net, frame, alpha, physics_weight, collocation):
    """Minimize telemetry squared error plus a soft simulator-consistency loss."""
    x = frame[FEATURES]
    features = encode(net, x)
    residual = frame.target_w.to_numpy() - net.predict(x)
    sample_weight = weights(frame).to_numpy()
    if physics_weight:
        synthetic_features, synthetic_residual = collocation
        features = np.vstack([features, synthetic_features])
        residual = np.concatenate([residual, synthetic_residual])
        sample_weight = np.r_[sample_weight, np.full(
            len(synthetic_residual), physics_weight * sample_weight.sum() / len(synthetic_residual))]
    return Ridge(alpha=alpha).fit(features, residual, sample_weight=sample_weight)


def score(frame, watts):
    data = frame[["track", "run"]].assign(
        measured_wh=frame.target_w * frame.dt_s / 3600,
        predicted_wh=watts * frame.dt_s / 3600,
        absolute_interval_error_j=np.abs(watts - frame.target_w) * frame.dt_s,
        seconds=frame.dt_s,
    )
    runs = data.groupby(["track", "run"]).sum()
    runs["signed_error_wh"] = runs.predicted_wh - runs.measured_wh
    runs["absolute_error_wh"] = runs.signed_error_wh.abs()
    runs["absolute_error_pct"] = [100 * error / measured if measured > 0 else None
                                  for error, measured in zip(runs.absolute_error_wh, runs.measured_wh)]
    columns = ["absolute_error_wh", "signed_error_wh", "absolute_error_pct"]
    return dict(**runs[columns].mean().to_dict(),
                interval_mae_w=float(data.absolute_interval_error_j.sum() / data.seconds.sum()),
                by_track=runs.groupby("track")[columns].mean().to_dict("index"),
                runs=runs.reset_index().drop(columns="absolute_interval_error_j").to_dict("records"))


def train(root, count, pretrained=None):
    field, sources = field_data(root)
    dev, test = field.loc[~field.holdout], field.loc[field.holdout]
    sim = synthetic(root, count)
    labels = physics(sim)
    folds = list(GroupKFold(n_splits=4).split(dev, groups=dev.run))
    candidates = []
    widths = (16, 32, 64) if pretrained is None else (pretrained.steps[-1][1].hidden_layer_sizes[0],)
    for width in widths:
        print(f"Evaluating width {width}", flush=True)
        net = network(width).fit(sim[FEATURES], labels) if pretrained is None else pretrained
        points = sim[FEATURES].iloc[::20]
        collocation = (encode(net, points), labels[::20] - net.predict(points))
        for alpha, physics_weight in ((a, w) for a in (0.01, 0.1, 1.0, 10.0)
                                      for w in (0.0, 0.003, 0.01, 0.03, 0.1)):
            fold_scores = []
            for fit, valid in folds:
                correction = fit_correction(net, dev.iloc[fit], alpha, physics_weight, collocation)
                model = dict(pretrained=net, correction=correction)
                validation = dev.iloc[valid]
                fold_scores.append(score(validation, predict_power(model, validation))["absolute_error_wh"])
            candidates.append((float(np.mean(fold_scores)), width, alpha, physics_weight, net, collocation))
    def refit(candidate):
        _, width, alpha, physics_weight, net, collocation = candidate
        return dict(pretrained=net, schema_version=1, correction=fit_correction(
            net, dev, alpha, physics_weight, collocation))

    informed = min((c for c in candidates if c[3] > 0), key=lambda c: c[0])
    selected = min(candidates, key=lambda c: (c[0], c[3]))
    ablation = min((c for c in candidates if c[3] == 0 and c[1] == informed[1]), key=lambda c: c[0])
    _, width, alpha, physics_weight, net, _ = selected
    model = refit(selected)
    x = test[FEATURES]
    real = network(width).fit(dev[FEATURES], dev.target_w, mlpregressor__sample_weight=weights(dev))
    forest = RandomForestRegressor(n_estimators=350, min_samples_leaf=20, max_depth=12,
                                  random_state=SEED, n_jobs=4)
    forest.fit(dev[FEATURES], dev.target_w, sample_weight=weights(dev))
    predictions = {
        "physics": physics(test),
        "pretrained_only": np.maximum(net.predict(x), 0),
        "real_only_mlp": np.maximum(real.predict(x), 0),
        "random_forest": forest.predict(x),
        "hybrid_selected": predict_power(model, test),
        "hybrid_physics_loss": predict_power(refit(informed), test),
        "hybrid_without_physics_loss": predict_power(refit(ablation), test),
    }
    report = dict(seed=SEED, sklearn=sklearn.__version__, synthetic_rows=len(sim),
                  selected=dict(width=width, alpha=alpha, physics_weight=physics_weight),
                  validation=[dict(mae_wh=mae, width=w, alpha=a, physics_weight=p)
                              for mae, w, a, p, _, _ in candidates],
                  sources=sources, metrics={name: score(test, pred) for name, pred in predictions.items()})
    return model, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pretrained-bundle", type=Path, help="Reuse a trusted stage-one network")
    parser.add_argument("--strategy", type=Path, help="Score a generated strategy segment CSV")
    parser.add_argument("--synthetic-count", type=int, default=40000)
    args = parser.parse_args()
    if args.synthetic_count < 1000:
        parser.error("--synthetic-count must be at least 1000")
    if args.strategy:
        model = joblib.load(args.output / "hybrid_energy.joblib")
        segments = pd.read_csv(args.strategy).rename(columns={
            "target_speed_kph": "speed_kph", "accel_demand_m_s2": "accel_m_s2",
            "baseline_grade_pct": "grade_pct", "baseline_curvature_1_m": "curvature_1_m",
        })
        durations = segments.segment_time_s.to_numpy()
        if not np.isfinite(durations).all() or (durations <= 0).any():
            parser.error("Strategy segment times must be finite and positive")
        watts = predict_power(model, segments)
        print(f"{np.dot(watts, durations) / 3600:.3f} Wh")
        return
    with threadpool_limits(limits=2):
        pretrained = joblib.load(args.pretrained_bundle)["pretrained"] if args.pretrained_bundle else None
        model, report = train(args.root, args.synthetic_count, pretrained)
    args.output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.output / "hybrid_energy.joblib", compress=3)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for name, result in report["metrics"].items():
        print(f"{name}: {result['absolute_error_wh']:.3f} Wh mean absolute run error")


if __name__ == "__main__":
    main()
