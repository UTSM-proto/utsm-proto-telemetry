# Large synthetic pretraining run

Use Python 3.12. Run these commands from the telemetry repository on the
`codex/hybrid-strategy-energy` branch.

```sh
python -m pip install -r requirements-hybrid-energy.txt
python -m utsm_telemetry.scale_hybrid_energy generate --dataset outputs/sim-100k --output outputs/train-100k --count 100000
python -m utsm_telemetry.scale_hybrid_energy pretrain --dataset outputs/sim-100k --output outputs/train-100k --epochs 3
python -m utsm_telemetry.scale_hybrid_energy finetune --dataset outputs/sim-100k --output outputs/train-100k
```

For the full run, use new directories such as `outputs/sim-1m` and
`outputs/train-1m`, set `--count 1000000` for generation and `--epochs 12`
for pretraining. These arguments specify total trajectories and total epochs.
Generation resumes from completed shards. Pretraining resumes from the last
completed epoch when you rerun the same command. Keep the dataset and output
directories on persistent disk. Run one writer per directory.

Each synthetic example is a complete smooth speed profile sampled on one of the
three track segment grids. Each shard stores speed, acceleration, duration,
grade, curvature, and simulator propulsion power with and without grade and
curvature. The trainer streams every segment each epoch and evaluates a
separate family of speed profiles. The current MLP learns neutral-context
power. The existing predictor adds the analytical grade and curvature
adjustment and fine-tunes its correction head on telemetry with the physics
loss. These are sampled trajectories under the default vehicle configuration.
Throttle commands, varied vehicle parameters, and a temporal network are
outside this implementation.

The trainer uses CPU and limits numerical library threads to four. Run the
100k pilot on your machine first and measure disk usage and time per epoch.
If you want the full run to continue unattended, use a persistent CPU VM and
run the commands in a persistent shell session. This trainer gains no
acceleration from renting a GPU. Keep `latest.joblib`, `best.joblib`,
`hybrid_energy.joblib`, both manifests and `report.json`.

Use development-run validation to compare corpus sizes before using fresh
held-out runs for promotion. The report uses PR #17's current field split and
baseline suite. A strategy-ranking test still requires logged commands and
independent driving runs. See [the model guide](HYBRID_ENERGY.md).
