"""Generate sharded simulator trajectories and pretrain a reusable power encoder."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from utsm_telemetry.hybrid_energy import FEATURES, SEED, network, train
from utsm_telemetry.simulation import build_motor_config, _physics_propulsion_power_w

ROOT = Path(__file__).resolve().parents[1]
SHARD_SIZE = 2048


def atomic_write(path, writer):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        writer(stream)
    os.replace(temporary, path)


def manifest(path, value):
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f"Configuration changed. Use a new output directory: {path.parent}")
    else:
        atomic_write(path, lambda stream: stream.write(json.dumps(value, indent=2).encode()))


def trajectories(track, count, rng, validation):
    length = track.length_m.to_numpy()
    n = len(length)
    # Sample distinct smooth speed profiles on the actual segment grid.
    phase = 2 * np.pi * np.arange(n) / n
    base = rng.uniform(14, 32, (count, 1))
    amplitude = rng.uniform(1, 6, (count, 1))
    harmonic = rng.integers(1, 4, (count, 1)) if not validation else 4
    speed = np.clip(base + amplitude * np.sin(harmonic * phase + rng.uniform(0, 2*np.pi, (count, 1)))
                    + rng.uniform(0, 2, (count, 1)) * np.cos(phase), 8, 39) / 3.6
    entry = np.roll(speed, 1, axis=1)
    average = (speed + entry) / 2
    dt = length / average
    accel = (speed**2 - entry**2) / (2 * length)
    motor = build_motor_config()
    power = _physics_propulsion_power_w(average * 3.6, accel, 0, motor)
    route_power = _physics_propulsion_power_w(
        average * 3.6, accel, track.baseline_grade_pct.to_numpy(), motor,
        curvature_1_m=track.baseline_curvature_1_m.to_numpy())
    return dict(speed_kph=(average * 3.6).astype("f4"), accel_m_s2=accel.astype("f4"),
                power_w=np.asarray(power, dtype="f4"), route_power_w=route_power.astype("f4"), dt_s=dt.astype("f4"),
                grade_pct=track.baseline_grade_pct.to_numpy(dtype="f4"),
                curvature_1_m=track.baseline_curvature_1_m.to_numpy(dtype="f4"))


def generate(root, directory, count):
    paths = sorted((root / "data/tracks").glob("*/*strategy-segments.csv"))
    if len(paths) != 3:
        raise ValueError("Expected three reference tracks")
    directory.mkdir(parents=True, exist_ok=True)
    config = dict(version=2, seed=SEED, train_count=count, shard_size=SHARD_SIZE,
                  sources={p.parent.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                  simulation_sha256=hashlib.sha256((ROOT / "utsm_telemetry/simulation.py").read_bytes()).hexdigest())
    manifest(directory / "manifest.json", config)
    for index, path in enumerate(paths):
        track = pd.read_csv(path)
        track_count = count // len(paths) + (index < count % len(paths))
        for split, total in [("train", track_count), ("validation", max(32, min(track_count // 10, 2048)))]:
            for start in range(0, total, SHARD_SIZE):
                name = directory / f"{split}-{path.parent.name}-{start:08}.npz"
                if not name.exists():
                    rng = np.random.default_rng([SEED, index, start, int(split == "validation")])
                    data = trajectories(track, min(SHARD_SIZE, total - start), rng, split == "validation")
                    atomic_write(name, lambda stream: np.savez_compressed(stream, **data))


def batches(path, rng=None):
    with np.load(path) as shard:
        x = np.column_stack([shard[name].ravel() for name in FEATURES])
        y = shard["power_w"].ravel()
        indices = rng.permutation(len(x)) if rng is not None else np.arange(len(x))
        for rows in np.array_split(indices, max(1, (len(x) + 8191) // 8192)):
            yield pd.DataFrame(x[rows], columns=FEATURES), y[rows]


def pretrain(directory, output, epochs):
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((directory / "manifest.json").read_text())
    manifest(output / "training-manifest.json", config)
    paths = sorted(directory.glob("train-*.npz"))
    validation = sorted(directory.glob("validation-*.npz"))
    if len(paths) != sum((config["train_count"] // 3 + (i < config["train_count"] % 3)
                          + SHARD_SIZE - 1) // SHARD_SIZE for i in range(3)) or len(validation) != 3:
        raise ValueError("Synthetic dataset is incomplete. Finish generate first.")
    checkpoint = output / "latest.joblib"
    if checkpoint.exists():
        state = joblib.load(checkpoint)
        net, history = state["pretrained"], state["history"]
    else:
        net, history = network(64), []
        scaler = net.steps[0][1]
        for path in paths:
            for x, _ in batches(path):
                scaler.partial_fit(x)
    scaler, mlp = net.steps[0][1], net.steps[1][1]
    mlp.set_params(batch_size=1024, learning_rate_init=0.001)
    for epoch in range(len(history), epochs):
        rng = np.random.default_rng([SEED, epoch])
        for path in rng.permutation(paths):
            for x, y in batches(path, rng):
                mlp.partial_fit(scaler.transform(x), y)
        errors = [np.abs(net.predict(x) - y)
                  for path in validation for x, y in batches(path)]
        mae = float(np.concatenate(errors).mean())
        history.append(mae)
        state = dict(pretrained=net, history=history)
        if mae == min(history):
            atomic_write(output / "best.joblib", lambda stream: joblib.dump(state, stream))
        atomic_write(checkpoint, lambda stream: joblib.dump(state, stream))
        print(f"Epoch {epoch + 1}/{epochs} | synthetic validation MAE {mae:.3f} W", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["generate", "pretrain", "finetune"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1_000_000)
    parser.add_argument("--epochs", type=int, default=12)
    args = parser.parse_args()
    if args.count < 3 or args.epochs < 1:
        parser.error("Use at least three trajectories and one epoch.")
    with threadpool_limits(limits=4):
        if args.stage == "generate":
            generate(ROOT, args.dataset, args.count)
        elif args.stage == "pretrain":
            pretrain(args.dataset, args.output, args.epochs)
        else:
            encoder = joblib.load(args.output / "best.joblib")["pretrained"]
            model, report = train(ROOT, 40000, encoder)
            report["pretraining_manifest"] = json.loads((args.output / "training-manifest.json").read_text())
            atomic_write(args.output / "hybrid_energy.joblib", lambda stream: joblib.dump(model, stream, compress=3))
            atomic_write(args.output / "report.json", lambda stream: stream.write(
                json.dumps(report, indent=2, allow_nan=False).encode()))
            for name, metrics in report["metrics"].items():
                print(f"{name}: {metrics['absolute_error_wh']:.3f} Wh")


if __name__ == "__main__":
    main()
