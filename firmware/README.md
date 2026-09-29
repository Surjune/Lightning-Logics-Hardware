# ESP32 transmitter firmware

Arduino sketch for the software-defined sonar transmitter (PS 26058). It synthesises
LFM chirps, geometric sweeps, Barker-13 phase-coded pulses and CW tones, streams them
by DMA to the ESP32's built-in 8-bit DAC at 2 MSPS on **GPIO25**, and adapts centre
frequency / bandwidth, pulse duration and amplitude to two potentiometers standing in
for environment sensors.

## How it meets the problem statement

| PS requirement | Implementation |
|---|---|
| Hardware timers + DMA, CPU not stalled | IDF `dac_continuous` driver: I2S0 clocks the DAC from PLL_D2 at 2 MHz and DMA reads a ring of 4 buffers. A task on core 0 refills each finished buffer with `memcpy`/`memset`; no per-sample CPU work ([dac_stream.cpp](sonar_tx/dac_stream.cpp)) |
| Fast wave computation | Direct digital synthesis: 64-bit phase accumulator + interpolated sine table. An LFM chirp is two integer adds per sample, with no trig calls ([pulse.cpp](sonar_tx/pulse.cpp)) |
| LFM, geometric, phase-coded on the fly | BOOT button cycles the modulation live; the serial `CFG`/`PRESET` commands select any pulse |
| ADC inputs change 3 parameters instantly | Pots on GPIO34/35 are read every 10 ms and change band, duration and amplitude (table below); new pulses swap in at the next ping boundary, so no ping mixes two waveforms |
| Digital windowing | Rect, Tukey, Hann, Hamming, Blackman; hold the BOOT button to cycle |
| Validated on an oscilloscope with FFT | Codes match the `sonarscope` reference model sample for sample ([tools/check_codes.py](tools/check_codes.py)); `sonarscope suite --dut serial` drives the firmware directly |

Not in this folder yet: the analog front end (4th-order low-pass filter + op-amp driver;
`sonarscope filter` prints the part values) and the enclosure.

## Build and upload (Arduino IDE)

1. Boards Manager: install **esp32 by Espressif Systems**, version **3.x** (tested with 3.3.12).
2. Open `firmware/sonar_tx/sonar_tx.ino`. The other files in that folder open as tabs.
3. Tools → Board → **ESP32 Dev Module**, pick the board's COM port, Upload.
4. Serial Monitor: **115200 baud**, line ending **Newline**. It prints `# sonar_tx ready ...`.

## Wiring

| ESP32 pin | Connect to | Notes |
|---|---|---|
| **GPIO25** (G25) | scope CH1 (or the filter input) | raw DAC: 0-3.3 V, idle at 1.65 V. Don't load it: ~10 kΩ or more |
| GND | scope ground clip | |
| GPIO27 (G27) | scope CH2 (optional) | T0 marker: steps to 3.3 V for 200 ms at every change |
| GPIO34 (G34) | 10 kΩ pot wiper: **turbidity** | pot ends to 3V3 and GND |
| GPIO35 (G35) | 10 kΩ pot wiper: **reach / range** | pot ends to 3V3 and GND |
| GPIO0 | on-board BOOT button | nothing to wire |

Without pots, set `USE_POTS` to 0 in [config.h](sonar_tx/config.h): floating inputs would
keep changing the pulse.

## Demo mode (power-up default)

| Pot position | Band | Pulse | Amplitude | Window (auto) |
|---|---|---|---|---|
| turbidity 0, reach 0: **clear shallow reef** | 400-500 kHz | 1 ms | 0.5 | Hann |
| turbidity 1: **muddy estuary** | 100-140 kHz | 5 ms | 1.0 | Tukey |
| turbidity 0, reach 1: clear water, long range | 400-500 kHz | 5 ms | 1.0 | Tukey |

Turbidity moves the band (sediment scatters high frequencies, so trade resolution for
penetration). The energy demand is the larger of turbidity and reach; it sets the pulse
length (1-5 ms) and amplitude (0.5-1.0), so a clear, close-range scene draws the least
power. Pings repeat every 20 ms. Each change prints a line such as

```
# ENV turbidity 0.80 reach 0.30 [MUDDY ESTUARY] -> lfm 162.0-197.0 kHz, 4.20 ms, amp 0.90, tukey | synth 5.1 ms
```

BOOT button: short press = next modulation (LFM → geometric → Barker-13 → CW), hold ≥ 0.6 s =
next window (auto → rect → tukey → hann → hamming → blackman).

## Serial commands

One command per line; every command gets one reply line starting with `OK` or `ERR`.
Lines starting with `#` are log output. Any command except STATUS/DUMP/HELP switches to
host mode (pots ignored) until `DEMO` or a BOOT press.

| Command | Effect |
|---|---|
| `PRESET lfm_hi` | load a sonarscope preset: `lfm_hi`, `lfm_hi_hann`, `lfm_hi_rect`, `lfm_lo`, `lfm_down`, `lfm_full`, `geometric`, `barker13`, `tone_101k`, `tone_250k`, `tone_487k`, `tone_500k`, `clear_reef`, `muddy_estuary` |
| `CFG {"kind":"lfm","f0":400000,"f1":500000,"duration":0.002,"window":"hann"}` | any PulseSpec (fields as in `sonarscope.waveforms.PulseSpec`; missing ones take its defaults) |
| `NEXT {...}` | same, through the adaptation path, with the T0 marker |
| `PRI 0.02` | ping repetition interval, seconds |
| `RUN` / `IDLE` | start / stop pinging (stops after the current ping, output at mid-scale) |
| `DEMO` | back to pot control |
| `STATUS` | current pulse, ping count, synthesis time, DMA underruns |
| `DUMP` | hex DAC codes of the pulse on air |

## Scope set-up (GW Instek GDS-1102-U, or any 2-channel DSO)

* CH1: DC coupling, probe/lead setting **1x** for a BNC clip lead (10x for a 10x probe), 500 mV/div,
  ground clip on the ESP32 GND. Idle sits at about 1.65 V.
* Trigger: CH1, rising edge, level about 2.0 V, mode **Normal** (not Auto), so the trace
  locks to each ping.
* Time/div: 500 µs/div shows a whole 1-5 ms pulse and its envelope; 2 µs/div shows cycles.
* FFT: **MATH → FFT**, source CH1, Hanning window. The chirp is a flat block from f0 to f1; turning
  the turbidity pot moves it between 400-500 kHz and 100-140 kHz.
* Edge demo: hold BOOT until `window: rect`. The pulse starts and stops with a hard step and
  the FFT grows side lobes. Hold again for tukey/hann: the edges ramp and the side lobes drop.

At GPIO25 you see the raw DAC: 0.5 µs steps and images around 1.5 MHz. The low-pass filter and
driver remove them; probe after the filter for the "clean" output the PS asks for.

## Checking the firmware against the reference model

```bash
pip install -e "scope[serial]"
python firmware/tools/check_codes.py COM7
```

This loads every sonarscope preset over serial and compares the DAC codes sample by
sample with `sonarscope.waveforms.dac_codes()`. Measured on an ESP32-WROOM-32 DevKit:
13 of 14 presets identical, `tone_101k` off by one code on 2 of 10 000 samples (a float
rounding tie); 0 DMA underruns; 2000 samples per DMA buffer.

Then run the bench suite with the firmware in control (see [scope/README.md](../scope/README.md)):

```bash
sonarscope suite --bench siglent --resource TCPIP::192.168.1.50::INSTR --dut serial --port COM7
```

## Timing

| | |
|---|---|
| DAC rate | 2 MSPS, PLL_D2 / 80 |
| Ping interval jitter | 0 samples: the PRI is counted in DAC samples |
| DMA pipeline | 4 buffers × 1 ms: output lags the refill task by ≤ 4 ms |
| Pulse change | synthesis + wait for the next ping boundary (≤ PRI) + ≤ 4 ms |

Synthesis time grows with pulse length; `STATUS` reports it (`synth_us`) and
`check_codes.py` prints it for every preset. Measured at 240 MHz:

| Pulse | Samples | Synthesis |
|---|---|---|
| clear_reef (1 ms, Hann) | 2000 | 1.5 ms |
| lfm_hi (2 ms, Tukey) | 4000 | 3.3 ms |
| barker13 | 1040 | 3.1 ms |
| geometric (2 ms) | 4000 | 8.3 ms (double-precision phase step) |
| muddy_estuary / lfm_lo (5 ms) | 10000 | 8.2 ms |

`plan.py` budgets 3 ms (`TestCase.synth_time`), so the transition budget is
10 + 3 + 20 = 33 ms. The worst case here, clear reef → muddy estuary, is 8.2 ms synthesis +
≤ 20 ms to the next ping + ≤ 4 ms DMA pipeline = 32.2 ms: inside the budget, with little margin.
