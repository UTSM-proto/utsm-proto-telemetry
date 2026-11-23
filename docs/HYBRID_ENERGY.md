# Hybrid strategy energy model

Train from the repository root using Python 3.12.

```sh
python -m pip install -r requirements-hybrid-energy.txt
python -m utsm_telemetry.hybrid_energy --output outputs/hybrid-energy
python -m utsm_telemetry.hybrid_energy --output outputs/hybrid-energy --strategy data/tracks/indianapolis/indianapolis-strategy-segments.csv
```

The first stage fits an MLP to the strategy simulator's analytical propulsive-power component using 40,000 sampled speed/acceleration states and the reference strategies' segment states. The second stage freezes the network and fits a regularized residual head to recorded battery draw. Its objective is duration-weighted squared error against measured power plus λ times squared error against analytical simulator power on synthetic states. The frozen encoder keeps stage two small. Grouped development validation selects λ, and a λ = 0 ablation uses the same encoder. This adapts the physics-loss principle from [Raissi et al.](https://doi.org/10.1016/j.jcp.2018.10.045) to an algebraic simulator. Grade and curvature enter at inference through the analytical power difference relative to flat, straight driving. Field labels contain no consistently aligned grade, curvature or throttle commands, so this transfer of the analytical adjustment is an assumption.

Width and residual regularization are selected on grouped development runs. The held-out partitions are September 20 at Laval, Front Campus run 03, and Indianapolis afternoon. Front Campus and Indianapolis holdouts share a day with their development runs. Use fresh days and logged commands for prospective strategy ranking. The random forest is refit on the same splits and inputs so it can be compared directly. The old saved lap-summary forest uses a different feature schema and target.

The report includes data hashes, accepted time, per-run Wh and per-track errors. The target uses the magnitude of recorded current multiplied by recorded voltage, without extra calibration, and trapezoidal interval integration. Laval and Front Campus use embedded wheel speed where valid. Indianapolis aligns telemetry to the GPX start using the existing repository clock-origin convention. Its GPS-derived motion and synchronization add uncertainty. Reported energy covers accepted moving intervals. It excludes rejected gaps and stationary draw. These results measure replay on observed driving and do not validate the energy ranking of untested strategies or absolute battery-meter accuracy.

The checked-in model uses the 64-wide encoder selected in the initial no-physics-loss sweep, then reuses it for the second sweep with Cynthia's physics term. The latter selected λ = 0.03 and ridge α = 0.1. The default command runs all three widths with the physics loss. To reproduce the checked-in report with the released encoder, add `--pretrained-bundle data/models/hybrid-energy/hybrid_energy.joblib`.

| Model | Mean absolute run error (Wh) |
| --- | ---: |
| Analytical propulsion physics | 20.907 |
| Synthetic pretraining only | 21.168 |
| MLP trained only on field data | 8.210 |
| Random forest trained on field data | 8.259 |
| Hybrid with physics loss | 9.379 |
| Same hybrid without physics loss | 9.435 |

The report covers three held-out runs, one per track. The hybrid improves on the physics baseline and narrowly improves on the no-physics-loss ablation, while both field-only baselines score lower overall. Treat this model as experimental.

The model bundle is a joblib artifact. Only load the checked-in bundle or one you trained locally.
