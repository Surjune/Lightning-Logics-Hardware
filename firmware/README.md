# ESP32 transmitter firmware

Arduino sketch for the software-defined sonar transmitter. It synthesises
LFM chirps, geometric sweeps, Barker-13 phase-coded pulses and CW tones, streams them
by DMA to the ESP32's built-in 8-bit DAC at 2 MSPS on **GPIO25**, and adapts band,
pulse length, amplitude, window, modulation and ping interval to the water through a
physics model.

## How it meets the requirements

| Requirement | Implementation |
|---|---|
| Hardware timers + DMA, CPU not stalled | IDF `dac_continuous` driver: I2S0 clocks the DAC from PLL_D2 at 2 MHz and DMA reads a ring of 4 buffers. A task on core 0 refills each finished buffer with `memcpy`/`memset`; no per-sample CPU work ([dac_stream.cpp](sonar_tx/dac_stream.cpp)) |
| Fast wave computation | Direct digital synthesis: 64-bit phase accumulator + interpolated sine table. An LFM chirp is two integer adds per sample, with no trig calls ([pulse.cpp](sonar_tx/pulse.cpp)) |
| LFM, geometric, phase-coded on the fly | The model picks LFM or Barker-13 automatically; the BOOT button, `MOD` or the dashboard force any of LFM, geometric, Barker-13, CW |
| ADC inputs change bandwidth / centre frequency, pulse duration and amplitude | Up to five pots (turbidity, range, temperature, salinity, depth) read every 10 ms feed the physics model ([env_model.cpp](sonar_tx/env_model.cpp)); new pulses swap in at the next ping boundary, so no ping mixes two waveforms |
| Digital windowing | Rect, Tukey, Hann, Hamming, Blackman; the model picks Hann or Tukey, or force one |
| Validated on an oscilloscope with FFT | Codes match the `sonarscope` reference model sample for sample ([tools/check_codes.py](tools/check_codes.py)); decisions match its physics model ([tools/check_adaptation.py](tools/check_adaptation.py)); `sonarscope suite --dut serial` drives the firmware directly |
| Live monitoring | The [dashboard](../dashboard/) shows the environment, the decision and the pulse read back from the board |

## Build and upload (Arduino IDE)

1. Boards Manager: install **esp32 by Espressif Systems**, version **3.x** (tested with 3.3.12).
2. Open `firmware/sonar_tx/sonar_tx.ino`. The other files in that folder open as tabs.
3. Tools → Board → **ESP32 Dev Module**, pick the board's COM port, Upload.
4. Serial Monitor: **115200 baud**, line ending **Newline**. It prints `# sonar_tx ready ...`.

## Wiring

| ESP32 pin | Connect to | Notes |
|---|---|---|
| **GPIO25** (G25) | scope CH1, or the analog front end ([hardware/](../hardware/)) | raw DAC: 0-3.3 V, idle at 1.65 V. Don't load it: ~10 kΩ or more |
| GND | scope ground clip | |
| GPIO27 (G27) | scope CH2 (optional) | T0 marker: steps to 3.3 V for 200 ms at every change |
| GPIO34 (G34) | pot wiper: **turbidity**, 0-100 NTU | wired by default |
| GPIO35 (G35) | pot wiper: **required range**, 5-200 m (log scale) | set `RANGE_POT_WIRED 1` (off by default: range stays at 60 m) |
| GPIO32 (G32) | pot wiper: temperature, 0-35 °C | set `TEMP_POT_WIRED 1` in [config.h](sonar_tx/config.h) |
| GPIO33 (G33) | pot wiper: salinity, 0-40 PSU | set `SALINITY_POT_WIRED 1` |
| GPIO36 (SP) | pot wiper: depth, 0-300 m | set `DEPTH_POT_WIRED 1` |
| GPIO0 | on-board BOOT button | nothing to wire |

Each pot is 10 kΩ with its ends on 3V3 and GND. An input without a pot uses its default
(0 NTU, 60 m, 25 °C, 35 PSU, 10 m) or a value set with `ENV` or the dashboard. Keep the
`*_WIRED` flag at 0 for any pin with nothing connected: a floating pin reads noise.

## Adaptive mode (power-up default)

The model ([env_model.cpp](sonar_tx/env_model.cpp), reference in
[scope/src/sonarscope/acoustics.py](../scope/src/sonarscope/acoustics.py)):

1. **Sound speed** from Mackenzie (1981); **seawater absorption** from Francois and Garrison
   (1982): boric acid, magnesium sulphate and pure water, with temperature, salinity and
   depth; **suspended sediment** as a linear-in-frequency term (100 NTU adds about
   400 dB/km at 500 kHz, an estimate to calibrate with a real turbidity sensor).
2. **Band:** the highest centre frequency from 450 kHz down to 120 kHz whose top edge keeps the
   two-way absorption over the required range within 20 dB. Higher frequency means finer range
   resolution; the water decides how high it can go. The swept bandwidth scales with it,
   from 400-500 kHz down to 100-140 kHz.
3. **Energy:** the two-way transmission loss (spherical spreading + absorption) across the
   operating envelope sets how much pulse energy to spend: first a longer pulse, up to 5 ms,
   while the blind zone stays within 10 % of the range, then a larger amplitude, 0.5 to 1.0.
4. **Modulation:** LFM, or Barker-13 when the blind-zone limit forces a pulse under 1 ms.
   **Window:** Hann for light pings (lowest range sidelobes), Tukey when energy matters.
5. **Ping interval:** 1.25 × the echo's round trip from the required range, at least 20 ms.

Some decisions, all at 25 °C, 35 PSU:

| Water | Range | Pulse | Interval | Range resolution |
|---|---|---|---|---|
| Clear | 20 m | LFM 400-500 kHz, 2.1 ms, amplitude 0.50, Hann | 33 ms | 0.8 cm |
| Clear | 60 m | LFM 389-486 kHz, 4.5 ms, 0.50, Tukey | 98 ms | 0.8 cm |
| 30 NTU | 60 m | LFM 193-252 kHz, 4.4 ms, 0.50, Hann | 98 ms | 1.3 cm |
| 100 NTU | 60 m | LFM 100-140 kHz, 4.4 ms, 0.50, Hann | 98 ms | 1.9 cm |
| 100 NTU | 200 m | LFM 100-140 kHz, 5 ms, 1.00, Tukey (range-limited) | 326 ms | 1.9 cm |
| 10 NTU | 5 m | Barker-13 on 450 kHz, 0.65 ms | 20 ms | 3.8 cm |

So the turbidity knob mainly moves the band and the range setting (`ENV range=...`, or the
optional G35 pot) mainly sets energy and ping rate. Each change prints a line such as

```
# ENV 30 NTU, 60 m, 25.0 C, 35.0 PSU, 10 m deep -> lfm 193.2-251.8 kHz, 4.42 ms, amp 0.50, hann, PRI 97.8 ms | res 1.3 cm | synth 6.49 ms
```

BOOT button: short press = next modulation (auto → LFM → geometric → Barker-13 → CW), hold
≥ 0.6 s = next window (auto → rect → tukey → hann → hamming → blackman).

## Serial commands

One command per line; every command gets one reply line starting with `OK` or `ERR`.
Lines starting with `#` are log output and `@` are telemetry.

| Command | Effect |
|---|---|
| `ENV turbidity=40 range=120` | set environment inputs (any of `turbidity`, `range`, `temp`, `salinity`, `depth`); they override the pots until `ENV AUTO`. `ENV` alone lists the values in use |
| `MOD auto` · `WIN auto` | modulation / window for adaptive mode: `auto` lets the model choose |
| `DEMO` | back to adaptive mode |
| `PRESET lfm_hi` | load a sonarscope preset (host mode: environment ignored): `lfm_hi`, `lfm_hi_hann`, `lfm_hi_rect`, `lfm_lo`, `lfm_down`, `lfm_full`, `geometric`, `barker13`, `tone_101k`, `tone_250k`, `tone_487k`, `tone_500k`, `clear_reef`, `muddy_estuary` |
| `CFG {"kind":"lfm","f0":400000,"f1":500000,"duration":0.002,"window":"hann"}` | any PulseSpec (fields as in `sonarscope.waveforms.PulseSpec`; missing ones take its defaults) |
| `NEXT {...}` | same, through the adaptation path, with the T0 marker |
| `PRI 0.02` | ping repetition interval, seconds |
| `RUN` / `IDLE` | start / stop pinging (stops after the current ping, output at mid-scale) |
| `TELEM ON` / `TELEM OFF` | `@{json}` status every 200 ms and on every change: environment, pulse (full precision, so it can be rebuilt exactly), physics, ping count |
| `STATUS` | current pulse, environment, ping count, synthesis time, DMA underruns |
| `DUMP` | hex DAC codes of the pulse on air |

## Scope set-up (GW Instek GDS-1102-U, or any 2-channel DSO)

* CH1: DC coupling, probe/lead setting **1x** for a BNC clip lead (10x for a 10x probe), 500 mV/div,
  ground clip on the ESP32 GND. Idle sits at about 1.65 V.
* Trigger: CH1, rising edge, level about 2.0 V, mode **Normal** (not Auto), so the trace
  locks to each ping.
* Time/div: 500 µs/div shows a whole 1-5 ms pulse and its envelope; 2 µs/div shows cycles.
* FFT: **MATH → FFT**, source CH1, Hanning window. Turning the turbidity pot moves the chirp's
  block between 400-500 kHz and 100-140 kHz.
* Edge demo: `WIN rect` (or hold BOOT until `window: rect`). The pulse starts and stops with a
  hard step and the FFT grows side lobes; `WIN hann` ramps the edges and the side lobes drop.

At GPIO25 you see the raw DAC: 0.5 µs steps and images around 1.5 MHz. The analog front end in
[hardware/](../hardware/) removes them; probe its output for the clean waveform.

## Checking the firmware against the reference model

```bash
pip install -e "./scope[serial]"
python firmware/tools/check_codes.py COM7
python firmware/tools/check_adaptation.py COM7
```

`check_codes.py` loads every sonarscope preset and compares the DAC codes sample by sample
with `sonarscope.waveforms.dac_codes()`. Measured on an ESP32-WROOM-32 DevKit: 13 of 14
presets identical, `tone_101k` off by one code on 2 of 10 000 samples (a float rounding tie);
0 DMA underruns; 2000 samples per DMA buffer.

`check_adaptation.py` (105 of 105 match on the DevKit) sets 105 environments and modulation / window combinations with `ENV`,
`MOD` and `WIN` and compares the board's decisions (band, pulse, amplitude, window, ping
interval, sound speed, absorption, transmission loss, energy) with
`sonarscope.acoustics.decide()`.

Then run the bench suite with the firmware in control (see [scope/README.md](../scope/README.md)):

```bash
sonarscope suite --bench siglent --resource TCPIP::192.168.1.50::INSTR --dut serial --port COM7
```

## Timing

| | |
|---|---|
| DAC rate | 2 MSPS, PLL_D2 / 80 |
| Ping interval jitter | 0 samples: the interval is counted in DAC samples |
| DMA pipeline | 4 buffers × 1 ms: output lags the refill task by ≤ 4 ms |
| Pulse change | synthesis + wait for the next ping boundary (≤ ping interval) + ≤ 4 ms |

Synthesis time grows with pulse length; `STATUS` reports it (`synth_us`) and
`check_codes.py` prints it for every preset. Measured at 240 MHz:

| Pulse | Samples | Synthesis |
|---|---|---|
| clear_reef (1 ms, Hann) | 2000 | 1.5 ms |
| lfm_hi (2 ms, Tukey) | 4000 | 3.3 ms |
| barker13 | 1040 | 3.1 ms |
| geometric (2 ms) | 4000 | 8.3 ms (double-precision phase step) |
| muddy_estuary / lfm_lo (5 ms) | 10000 | 8.2 ms |

In host mode with the test plan's 20 ms interval, `plan.py` budgets 10 + 3 + 20 = 33 ms for a
change (`TestCase.synth_time` = 3 ms). The worst case here, clear reef → muddy estuary, is
8.2 ms synthesis + ≤ 20 ms to the next ping + ≤ 4 ms DMA pipeline = 32.2 ms: inside the budget,
with little margin. In adaptive mode the ping interval grows with range (up to 326 ms at
200 m), and a change lands at the next ping, as it must: the transmitter cannot change a ping
whose echo is still on its way back.
