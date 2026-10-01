# sonarscope

Oscilloscope / FFT validation toolkit for the software-defined sonar transmitter.
It turns scope captures of the transmitter output into measured,
pass/fail evidence: clean envelopes, linear sweeps, suppressed reconstruction
images, low distortion, low range sidelobes, stable ping timing, and glitch-free
adaptation when the environment input changes.

```
waveforms ─► chain simulator ─┐
                              ├─► capture ─► measure ─► thresholds ─► report.html / results.json
real scope (SCPI / AD3) ──────┘                                       └─► regression vs. history
```

## Install

```bash
cd scope
python -m pip install -e ".[dev]"          # core + tests
python -m pip install -e ".[visa,serial]"  # add for real instruments / firmware link
```

## Quick tour (no hardware needed)

```bash
sonarscope selfcheck                         # prove the analyzer first (39 checks)
sonarscope suite --out out/reference         # full test plan on the reference model
open out/reference/report.html               # plots + criteria tables, single file
sonarscope suite --fault filt=none --only lfm_hi,tone_487k   # see a broken chain fail
```

`selfcheck` runs three kinds of check on the reference model of the transmit chain:

| Group | What it proves |
|---|---|
| reference | the analyzer reproduces independently computed design figures: Hann-weighted chirp sidelobes -46.7 dB, Tukey chirp with Hamming-weighted replica -31.1 dB, 400-500 kHz image rejection -41.8 dBc, THD -40.3 dB for a 500 kHz tone (image on H3), Barker-13 -16.5 dB, sweep R² ≥ 0.999 |
| clean chain | every test in the default plan passes on a healthy chain |
| discrimination | each injected fault is caught by the test aimed at it: missing filter, slew-limited driver, swapped DMA sample pairs, supply ripple, output offset, mixed ping after a buffer swap, untapered pulse, DAC bow, overdriven scope input; 5 µs trigger jitter must *not* cause a failure |

A checker that passes everything is worthless; the discrimination group is what
makes a PASS on the bench mean something.

## Bench workflow

### 1. Instrument and floor

An 8-bit oscilloscope has about 48 dB of dynamic range, roughly the size of the
spurs being measured. Prefer a 12-bit scope (Siglent SDS800X HD / SDS1000X HD class)
or a Digilent Analog Discovery. The `floor` test measures the instrument's own
noise/spur floor; spur-type criteria that fail within 10 dB of that floor are
reported **INCONCLUSIVE**, not FAIL: the scope, not the transmitter, may be what was measured.

### 2. Test points and probing

| Point | Node | Look for |
|---|---|---|
| TP1 | ESP32 DAC pin (GPIO25) | raw 8-bit steps, strong images |
| TP2 | buffer output (U1A) | a copy of TP1, idle at about 1.65 V |
| TP3 | after the 4th-order reconstruction filter | images suppressed |
| TP4 | driver output across the dummy load | the deliverable; slew/drive problems show here |

10x probe, compensated on the calibrator; short ground spring, not the long lead.
Fill 70-90 % of the screen vertically. The `clip_runs` check fails if the input is
overdriven.

### 3. Test plan

`sonarscope presets` prints the plan with set-up instructions. Default plan:

| Test | Pulse | Checks |
|---|---|---|
| floor | none, probe shorted | instrument floor ≤ -48 dBc of full scale |
| idle | none, transmitter idle | DC offset ≤ 20 mV |
| tone_101k / 250k / 487k | CW tones | THD ≤ -34 dB, SFDR ≤ -38 dBc |
| tone_500k | CW 500 kHz | informational: at 2 MSPS the first image (1.5 MHz) lands on H3, so the scope reports filter leakage as THD |
| lfm_lo | LFM 100-140 kHz, 5 ms, Tukey | edge, sweep, images ≤ -50 dBc, weighted PSL |
| lfm_hi / lfm_down | LFM 400-500 kHz (up / down) | edge, sweep, images ≤ -38 dBc, weighted PSL |
| lfm_hi_hann | LFM 400-500 kHz, Hann | matched PSL ≤ -35 dB |
| lfm_hi_rect | LFM, no window | reference: the edge step is expected to fail |
| lfm_full | LFM 100-500 kHz | full-band sweep |
| geometric | exponential sweep 100-500 kHz | log-linear sweep fit |
| barker13 | Barker-13 BPSK on 300 kHz | matched PSL ≤ -15 dB |
| pri | 4 pings, 20 ms PRI | PRI jitter ≤ 0.5 µs (one DAC sample) |
| transition | clear reef → muddy estuary | no mixed / unknown / reverted pings; latency within ADC period + synthesis + one PRI |

### 4. Pass/fail criteria

Limits are engineering targets from the reference model with margin for real
hardware; each criterion also carries a tighter target. THD/SFDR/sine-fit
methodology follows IEEE Std 1241 and IEEE Std 1057. No published chirp
standard was found, so these limits are the project's own.

| Metric | Limit | Target | Meaning |
|---|---|---|---|
| `edge_step_pct` | ≤ 5 % | ≤ 2 % | envelope jump within 3 µs of the pulse edges |
| `window_rms_error` | ≤ 0.05 | ≤ 0.03 | envelope shape vs commanded window, in-band droop removed |
| `symmetry` | ≥ 0.98 | ≥ 0.99 | rising vs falling edge |
| `rise_ratio`, `fall_ratio` | 0.8-1.25 | 0.9-1.1 | measured / commanded 10-90 % ramp |
| `freq_r2` | ≥ 0.998 | ≥ 0.999 | instantaneous-frequency fit (log-frequency for geometric sweeps) |
| `sweep_rate_error_pct`, `f_start_error_pct`, `f_end_error_pct` | ≤ 1 % | ≤ 0.5 % | sweep rate and end points |
| `bw10_error_pct` | ≤ 5 % | ≤ 2 % | -10 dB bandwidth vs the commanded waveform |
| `droop_db` | ≥ -1.5 dB | ≥ -0.5 dB | amplitude at the top of the sweep vs mid-band |
| `image_dbc` | ≤ -38 dBc (hi band), ≤ -50 dBc (lo band) | -42 / -60 | worst spur above 1 MHz |
| `thd_db` / `sfdr_dbc` | ≤ -34 dB / ≤ -38 dBc | -40 / -42 | tones |
| `psl_matched_db` | ≤ -35 dB (Hann), ≤ -15 dB (Barker) | -42 / -18 | matched-filter peak sidelobe |
| `psl_weighted_db` | ≤ -28 dB | ≤ -30 dB | Tukey pulse, Hamming-weighted replica |
| `pri_jitter_pp_s` | ≤ 0.5 µs | ≤ 0.1 µs | ping-to-ping interval |
| `n_mixed`, `n_unknown`, `n_reverted` | 0 | 0 | adaptation glitches |
| `latency_s` | ≤ budget | | input change to first new ping |
| `idle_dc_v` | ≤ 20 mV | ≤ 5 mV | output offset while idle |
| `clip_runs` | 0 | 0 | scope input overdriven |
| `floor_dbc` | ≤ -48 dBc | ≤ -60 dBc | instrument floor |

Verdicts: **PASS**, **FAIL**, **INCONCLUSIVE** (spur failure within 10 dB of the instrument
floor), **EXPECTED-FAIL** (reference cases), **INFO** (demonstrations), **SKIPPED** (bench cannot run it).

### 5. Running on real instruments

```bash
# Siglent / Rigol over LAN (pyvisa-py) with the operator setting up each test
sonarscope suite --bench siglent --resource TCPIP::192.168.1.50::INSTR --dut manual --out out/bench1
sonarscope suite --bench rigol   --resource TCPIP::192.168.1.51::INSTR --dut manual

# Firmware test mode over serial: the suite configures every pulse itself
sonarscope suite --bench siglent --resource TCPIP::192.168.1.50::INSTR --dut serial --port /dev/ttyUSB0

# Analog Discovery (single-pulse tests; multi-ping tests are skipped: buffer too short)
sonarscope suite --bench ad3 --dut manual

# One-off capture, then analysis
sonarscope capture --scope siglent --resource TCPIP::192.168.1.50::INSTR --record 3e-3 -o hi.npz
sonarscope analyze hi.npz --test lfm_hi --plot hi.png

# Scope CSV exports (Rigol-style, Siglent-style and plain time,volt layouts)
sonarscope analyze export.csv --preset lfm_hi
```

Channel use: CH1 = signal. For the `transition` test CH2 = the T0 marker (preset
button or the firmware's marker GPIO); the capture triggers on T0 and keeps
`t_change` of history before it, so pings before and after the change are recorded.

Pulses are located by correlating with the replica, so trigger position and
trigger jitter do not affect any measurement. For averaging on the scope itself,
trigger on the signal, not on a GPIO: 1 µs of trigger jitter is 180° at 500 kHz.

### 6. Firmware test-mode protocol (`--dut serial`)

One command per line, one reply line (`OK` or `ERR <message>`) per command:

| Command | Meaning |
|---|---|
| `CFG {"kind":"lfm","f0":400000.0,"f1":500000.0,"duration":0.002,"window":"tukey","tukey_alpha":0.2,"amplitude":1.0,...}` | select the pulse (PulseSpec JSON, SI units) |
| `PRI 0.02` | ping repetition interval, seconds |
| `RUN` / `IDLE` | start pinging / stop with the DAC at mid-scale |
| `NEXT {...}` | request a change to a new pulse through the normal adaptation path (back buffer, swap at the next ping boundary) and raise the T0 marker GPIO at the moment of the request |

`kind` is one of `lfm`, `geometric`, `barker13`, `cw`; `window` one of `rect`,
`tukey`, `hann`, `hamming`, `blackman`. DAC codes are `128 + round(127 * amplitude *
window * waveform)` at 2 MSPS; `sonarscope.waveforms.dac_codes(spec)` gives the exact
sequence the firmware should produce.

### 7. Regression tracking

```bash
sonarscope suite --bench siglent --resource ... --history results/
```

Each run is recorded as `results/<revision>_<timestamp>.json` and compared with the
previous one; metrics that got worse beyond tolerance (1 dB for dB figures, 0.25 %
for percentages, 5e-4 for R²) and PASS→FAIL changes are listed and the command exits
non-zero. `sonarscope compare old.json new.json` compares any two runs.

## Design notes the measurements encode

* **The ESP32 DAC runs at 2 MSPS**, so a 500 kHz tone has 4 samples per cycle and its
  first image sits at 1.5 MHz. The 4th-order 600 kHz Butterworth filter plus the
  DAC hold give about -42 dBc there, close to the 8-bit noise floor, so the image limit is
  the tightest spec in the plan. `sonarscope filter` prints the stage values
  (R1 1.30 kΩ / R2 511 Ω / 390 pF / 270 pF and R1 1.74 kΩ / R2 715 Ω / 680 pF / 82 pF).
* **Test THD off 500 kHz.** At exactly fs/4 the image lands on H3 and is counted as
  distortion; 487.3 kHz separates them.
* **Tukey on transmit does not lower range sidelobes** (-13.4 dB, same as no window).
  Hann on transmit gives -47 dB but costs 4.3 dB of pulse energy; a Tukey pulse with a
  Hamming-weighted receive replica gives -31 dB for about 1.5 dB total.
* **Tapering a Barker burst costs sidelobe level**: a 5 % taper keeps about -19.6 dB but
  leaves an edge step of about 18 %; the 12 % taper used here gives -16.5 dB with a 2.5 % step.
* **A DAC bow shows up as an H2 spur (SFDR)** before it pushes THD over its limit.

## Module map

| Module | Role |
|---|---|
| `waveforms` | pulse specs, analytic waveforms and windows, DAC code generation, presets |
| `acoustics` | sound speed, seawater and sediment absorption, and the transmitter's adaptation rule (reference for the firmware) |
| `afe` | Sallen-Key filter design (E96 parts), DAC hold response |
| `chain` | transmit-chain simulator with fault models, scope model, transition schedule |
| `capture` | capture container; npz, generic / Rigol / Siglent CSV |
| `dsp`, `measure` | alignment, envelopes and every metric |
| `thresholds`, `plan` | criteria and the test plan |
| `analyze`, `suite` | per-test analysis and plan runner |
| `plots`, `report` | diagnostic PNGs, single-file HTML report |
| `regression` | history and comparison |
| `backends` | simulated bench, file replay, Rigol/Siglent SCPI, Analog Discovery, instrument bench |
| `dut` | manual and serial control of the transmitter |
| `selfcheck`, `cli` | analyzer verification, `sonarscope` command |

## Status and limits

* Measurements, thresholds, report and regression are verified against the reference
  model (`pytest`, `sonarscope selfcheck`).
* The Rigol DS1000Z and Siglent SDS drivers follow the vendors' programming guides
  and are tested against simulated instruments that emit the documented formats;
  the Analog Discovery driver follows the WaveForms SDK reference. None has been run
  against physical hardware yet. On the first bench session, capture a known sine
  from a function generator and confirm amplitude, frequency and trigger time before
  trusting a full run. Timebase delay sign conventions differ between models; the
  analysis does not depend on trigger position, but the plots' time axis does.
  For 12-bit Siglent captures also confirm the sample byte order: the driver follows the
  SDS guide (`COMM_ORDER` 0 = LSB first, the default); a wrong order shows up as noise-like
  garbage instead of the known sine.
* The CSV importer handles the common Rigol-style (`X,CH1,Start,Increment`) and
  Siglent-style (key/value preamble, `Second,Value`) layouts; export formats vary by
  model and firmware, so check the first export from your scope with `sonarscope analyze`.
* The simulated chain models the DAC as ideal apart from the injected faults; real
  ESP32 DAC nonlinearity and output impedance are what the bench measures.
