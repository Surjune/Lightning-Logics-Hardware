# Sonar-Transmitter

Low-power, real-time adaptive software-defined sonar transmitter payload for AUVs
(Problem Statement 26058 — MoES / NIOT).

The transmitter synthesises LFM chirps, geometric sweeps and phase-coded pulses on an
ESP32 (I2S0 → built-in 8-bit DAC via DMA), reconstructs them through an active
4th-order low-pass filter and a high-slew driver, and adapts centre frequency,
bandwidth, pulse length and amplitude to environmental inputs.

## Repository layout

| Path | Contents |
|---|---|
| [`scope/`](scope/) | `sonarscope` — oscilloscope / FFT validation toolkit: waveform reference models, transmit-chain simulator, capture I/O, measurement suite, pass/fail thresholds, instrument backends, regression tracking |
| [`firmware/`](firmware/) | ESP32 transmitter firmware (Arduino): DMA-fed DAC, LFM / geometric / Barker-13 / CW synthesis, pot-driven adaptation, serial test-mode protocol |
| `hardware/` | Analog front-end schematics, BOM, enclosure (planned) |

## Quick start (validation toolkit)

```bash
cd scope
python -m pip install -e ".[dev]"
sonarscope selfcheck        # verifies the analyzer against the reference model
pytest                      # unit tests
```

See [`scope/README.md`](scope/README.md) for the bench workflow.
