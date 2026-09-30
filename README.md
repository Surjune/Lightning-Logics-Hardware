# Sonar-Transmitter

**An adaptive, software-defined sonar transmitter for underwater drones (AUVs).**
Smart India Hackathon, Problem Statement 26058 (MoES / NIOT), category Hardware.

An ESP32 microcontroller reads the water conditions (how muddy it is, how far the sonar must
see, temperature, salinity, depth), works out the best sonar "ping" for those conditions with
real underwater-acoustics formulas, and plays that ping out of its DAC at 2 million samples per
second. You can watch every ping on an oscilloscope, and a browser dashboard shows the decision
and the waveform live.

| | Status |
|---|---|
| ESP32 firmware: waveform synthesis, DMA output, physics-based adaptation | Working on hardware |
| Firmware output checked against an independent reference model | 14 test pulses bit-exact except 2 samples off by one step; 105 of 105 adaptation decisions match |
| Browser dashboard | Simulator mode tested; live mode connects over USB (Chrome / Edge) |
| Analog front end (filter + amplifier) | Designed and simulated, ready to build: [hardware/](hardware/) |
| Enclosure, transducer power amplifier | Not started |

---

## 1. The problem in plain words

Side-scan sonar on an AUV sends out short sound pulses ("pings") and listens for echoes to draw
a picture of the seabed. The best ping depends on the water:

* **High frequency (around 500 kHz)** gives sharp images, but the water absorbs it quickly, and
  mud and silt absorb it even more.
* **Low frequency (around 100 kHz)** travels further through murky water, but the image is blurrier.

A normal sonar uses one fixed ping everywhere. The problem statement asks for a transmitter that
behaves like a software-defined radio: it senses the water and changes its ping in real time,
while using as little battery as possible.

## 2. How this project solves it

```mermaid
flowchart LR
    A["Water sensors<br>(pots stand in for them)"] --> B["ESP32 ADC<br>every 10 ms"]
    B --> C["Physics model<br>picks band, length,<br>power, ping rate"]
    C --> D["Waveform synthesis<br>LFM / geometric /<br>Barker-13 / CW"]
    D --> E["DMA + I2S clock<br>2 MSPS, no CPU"]
    E --> F["8-bit DAC<br>GPIO25"]
    F --> G["Low-pass filter<br>+ op-amp driver"]
    G --> H["Transducer<br>or oscilloscope"]
```

1. **Sense.** Potentiometers stand in for sensors, as the problem statement allows. Each gives a
   voltage of 0-3.3 V that the ESP32 reads as turbidity (0-100 NTU), required range (5-200 m),
   and optionally temperature, salinity and depth. The same values can also be typed in or set from
   the dashboard.
2. **Decide.** A physics model computes how strongly this water absorbs each frequency
   (Francois-Garrison seawater absorption plus a term for suspended mud) and the speed of sound
   (Mackenzie equation). It then picks:
   * the **highest frequency band** the water lets through over the required range, because higher
     frequency means sharper images;
   * the **pulse length and amplitude**, so the ping carries enough energy for the range and no more;
   * the **modulation** (LFM chirp normally, Barker-13 at very short range) and the **window**;
   * the **ping interval**, long enough for the echo to come back before the next ping.
3. **Synthesise.** The firmware computes the new pulse as 8-bit DAC values with a phase
   accumulator (direct digital synthesis), without calling sin() per sample.
4. **Stream.** A hardware clock (I2S) and DMA feed those values to the DAC at exactly 2 MHz. The
   CPU only refills a buffer once per millisecond, so it is free, and the ping timing never jitters.
   A new pulse is only swapped in between pings, so no ping is ever half old and half new.
5. **Condition.** The DAC's output is a fine staircase. A 4th-order low-pass filter smooths it and
   an op-amp drives the load ([hardware/](hardware/)).

What the model decides, for a few kinds of water (25 °C seawater):

| Water | Range | Ping chosen | Ping interval | Image sharpness (range resolution) |
|---|---|---|---|---|
| Clear | 20 m | LFM 400-500 kHz, 2.1 ms, half power, Hann window | 33 ms | 0.8 cm |
| Clear | 60 m | LFM 389-486 kHz, 4.5 ms, half power, Tukey window | 98 ms | 0.8 cm |
| Coastal, 30 NTU | 60 m | LFM 193-252 kHz, 4.4 ms, half power | 98 ms | 1.3 cm |
| Muddy, 100 NTU | 60 m | LFM 100-140 kHz, 4.4 ms, half power | 98 ms | 1.9 cm |
| Muddy, 100 NTU | 200 m | LFM 100-140 kHz, 5 ms, full power | 326 ms | 1.9 cm |
| Harbour, 5 m range | 5 m | Barker-13 on 450 kHz, 0.65 ms | 20 ms | 3.8 cm |

In short: **the turbidity knob moves the frequency band, and the range setting sets the energy and
ping rate.** Range comes from `ENV range=...` or from an optional second pot.

### The waveforms

| Waveform | What it is | Why sonar uses it |
|---|---|---|
| **LFM chirp** | frequency rises steadily across the pulse | a long pulse carries energy, yet the echo compresses to a sharp peak |
| **Geometric sweep** | frequency rises exponentially | behaves the same at every frequency ratio, tolerant of Doppler |
| **Barker-13** | a tone whose phase flips in a fixed 13-step code | short and compressible: good at close range |
| **CW tone** | one steady frequency | simple; used for distortion tests |

**Windowing** fades each pulse in and out instead of switching it on and off abruptly. That protects
the transmitter from voltage jumps and lowers the false echoes ("sidelobes") next to real targets:
with a Hann window the strongest sidelobe drops from −13 dB to −47 dB.

## 3. Problem-statement requirements and where they are met

| Requirement | Where |
|---|---|
| Firmware on a microcontroller, in C/C++ | [firmware/sonar_tx/](firmware/sonar_tx/) |
| Hardware timers and DMA stream to the DAC without stalling the CPU | [dac_stream.cpp](firmware/sonar_tx/dac_stream.cpp) |
| LFM chirps, geometric sweeps and phase-coded pulses, switched on the fly | [pulse.cpp](firmware/sonar_tx/pulse.cpp); BOOT button, dashboard or `MOD` command |
| Environment read by the ADC changes bandwidth / centre frequency, pulse duration and amplitude | [env_model.cpp](firmware/sonar_tx/env_model.cpp) |
| Digital windows (Hamming, Hann, Blackman) | [pulse.cpp](firmware/sonar_tx/pulse.cpp) |
| Analog low-pass filter and op-amp | [hardware/](hardware/) |
| Clean waveform and FFT on an oscilloscope | Section 5 below; [scope/](scope/) measures it automatically |
| Low power | DMA streaming leaves the CPU idle; the model spends pulse energy only when the water needs it (a clear, close-range ping carries about 1/47 of the energy of a muddy, long-range one) |
| Enclosure | not started |

## 4. What is in this repository

| Folder | What it is | Start with |
|---|---|---|
| [`firmware/`](firmware/) | The ESP32 program (Arduino IDE) and two tools that check the board's output | [firmware/README.md](firmware/README.md) |
| [`dashboard/`](dashboard/) | One web page that connects to the board over USB and shows everything live, or simulates it without a board | [dashboard/README.md](dashboard/README.md) |
| [`hardware/`](hardware/) | Schematic, parts list and build guide for the filter and amplifier board | [hardware/README.md](hardware/README.md) |
| [`scope/`](scope/) | `sonarscope`, a Python toolkit: reference models of every waveform, the physics model, and automatic pass/fail analysis of oscilloscope captures | [scope/README.md](scope/README.md) |

---

## 5. Build it yourself, step by step

### Step 0: what you need

**To see the pulses (about an hour):**

* ESP32 DevKit, 38-pin, ESP32-WROOM-32 module (any board whose chip is a plain ESP32; the S2, S3
  and C3 variants have no suitable DAC)
* micro-USB cable that carries **data**, not just power
* a 2-channel digital oscilloscope with an FFT (MATH) function; a 12-bit scope shows the small
  distortion products better than an 8-bit one
* BNC-to-crocodile-clip leads, or better, 10x scope probes
* 2 × 10 kΩ linear potentiometers, a breadboard and jumper wires
* a PC with [Arduino IDE 2](https://www.arduino.cc/en/software), Python 3.10 or newer, and Chrome or Edge

**For the full transmitter:** the parts in [hardware/bom.csv](hardware/bom.csv). The key part is a
**fast op-amp**, OPA4350 (or MCP6294 on a breadboard). Slow op-amps such as the LM358, LM324 or
uA741 will not work at 500 kHz.

### Step 1: put the firmware on the ESP32

1. Arduino IDE → **Boards Manager** → install **esp32 by Espressif Systems**, version **3.x**.
2. **File → Open** → `firmware/sonar_tx/sonar_tx.ino`. All eight files in that folder open as tabs; keep them together.
3. **Tools → Board → ESP32 Dev Module**, and **Tools → Port** → the ESP32's COM port
   (it appears as *Silicon Labs CP210x* or *CH340*).
4. Press **Upload** (→).
5. **Tools → Serial Monitor**, **115200 baud**, line ending **Newline**. Press the board's EN/RST
   button and you should see:

   ```
   # sonar_tx ready: GPIO25 DAC out @ 2 MSPS, T0 marker on GPIO27
   # adaptive mode, pots: turbidity GPIO34 (the rest: defaults or ENV, see config.h); BOOT = next modulation, hold = next window
   # type HELP for the serial commands
   # ENV 0 NTU, 60 m, 25.0 C, 35.0 PSU, 10 m deep -> lfm 388.6-486.4 kHz, 4.51 ms, amp 0.50, tukey, PRI 97.8 ms | res 0.8 cm | synth 7.49 ms
   ```

   The last line is the first decision: the environment on the left, the chosen ping on the right.
   By default only the turbidity pot (G34) is read, and range stays at 60 m. Until the pot is wired
   (Step 2) G34 floats, so its NTU number may differ from this line or wander; that is expected.
   Type `ENV turbidity=100 range=200` (muddy water, long range) and the board answers with a new
   decision. `ENV AUTO` hands control back to the pot.

   ```
   # ENV 100 NTU, 200 m, 25.0 C, 35.0 PSU, 10 m deep -> lfm 100.0-140.0 kHz, 5.00 ms, amp 1.00, tukey, PRI 325.8 ms | res 1.9 cm | range-limited | synth 8.20 ms
   ```

### Step 2: wire the knobs (the "sensors")

A potentiometer has three pins. The two outer pins go to **3V3** and **GND**; the middle pin
(the wiper) gives 0-3.3 V as you turn it.

| Pot | Middle pin to | Outer pins to | Stands in for |
|---|---|---|---|
| RV1 | **G34** | 3V3 and GND | turbidity, 0-100 NTU |
| RV2 (optional) | **G35** | 3V3 and GND | required range, 5-200 m |

Rules: use **3V3, never 5V** (the ADC pins take 3.3 V at most), and never connect 3V3 straight to GND.
Only the middle pin may go to G34: a pot with GND on its middle pin reads 0 and can short 3V3 to
GND at the end of its travel.

One pot (RV1) is enough, and it is the default: range stays at 60 m, or set it with
`ENV range=...`. RV2 and pots for temperature (G32), salinity (G33) and depth (SP / GPIO36) are
optional: set their `*_WIRED` flags to 1 in [config.h](firmware/sonar_tx/config.h). Keep a flag at 0
for any pin with nothing connected, because a floating pin reads noise.

Turn RV1 and the Serial Monitor prints a new `# ENV ...` line each time the decision changes.

### Step 3: connect the oscilloscope

| Scope | Connect to | Setting |
|---|---|---|
| CH1 tip | **G25** (the DAC output) | DC coupling, 500 mV/div, probe **1X** for a clip lead (10X for a 10x probe) |
| CH1 ground | ESP32 **GND** | |
| CH2 (optional) | **G27** (marker: goes high for 200 ms at every change) | DC, 2 V/div |
| Trigger | CH1, rising edge, about 2.0 V, mode **Normal** | |

The output idles at about 1.65 V (half of 3.3 V) and swings around it during a ping.

### Step 4: what you should see

Type these in the Serial Monitor. `PRESET` loads a fixed test pulse; `DEMO` goes back to
adaptive mode.

| Type | Time/div | You should see |
|---|---|---|
| `PRESET clear_reef` | 250 µs | a 1 ms burst with a smooth spindle-shaped envelope (Hann window) |
| same, then **MATH → FFT** | 100 µs | a block of energy between 400 and 500 kHz |
| `PRESET muddy_estuary` | 1 ms | a 5 ms burst at full height |
| same, FFT | 250 µs | the block has moved down to 100-140 kHz |
| `PRESET lfm_hi_rect`, then `PRESET lfm_hi_hann` | 250 µs | hard on/off edges, then smooth ramps: the effect of windowing |
| `PRESET barker13` | 50 µs | a 520 µs burst with small dips where the phase flips |
| `PRESET geometric` | 250 µs | cycles that bunch up towards the end |
| `PRESET lfm_hi` | 5 ms | pings repeating every 20 ms, perfectly evenly |
| `PRESET tone_250k` | 2 µs | a sine; the scope's frequency readout shows 250 kHz |
| `DEMO`, then turn RV1 | 1 ms | the pulse changes shape and the FFT block slides as the "water" gets muddier |

At G25 the waveform looks slightly jagged at 400-500 kHz, because the DAC produces only 4-5
samples per cycle. That is expected. The filter board in step 6 smooths it, and probing after the
filter shows the clean sine-like waveform the problem statement asks for.

If the FFT peak appears at the wrong frequency, the scope is sampling too slowly: lower the
time/div by one step.

### Step 5: the dashboard

```bash
cd Sonar
python -m http.server 8000
```

Open <http://localhost:8000/dashboard/> in **Chrome or Edge**. Close the Arduino Serial
Monitor (only one program can use the port), press **Connect board** and pick the ESP32's port.
The sliders act as sensors. The page shows the decision, the physics behind it, and the pulse
read back from the board with its spectrum, spectrogram and matched-filter output, and it
reports whether the board's codes match the model. **Simulate** runs everything in the browser
without a board. Details: [dashboard/README.md](dashboard/README.md).

### Step 6: build the analog front end

Follow [hardware/README.md](hardware/README.md): one quad op-amp on the ESP32's 5 V pin, a buffer,
two Sallen-Key filter stages (600 kHz, 4th-order Butterworth) and a driver into a 1 kΩ dummy load.
It includes the schematic, parts list, pin-by-pin wiring list and the readings to expect at each
test point. Expected result: flat to 250 kHz, −0.9 dB at 500 kHz, and the DAC's 1.5 MHz image
about 42 dB down.

### Step 7: check everything with the Python tools (optional)

```bash
pip install -e "./scope[dev,serial]"
python firmware/tools/check_codes.py COM7        # the board's DAC codes vs the reference waveforms
python firmware/tools/check_adaptation.py COM7   # the board's decisions vs the reference physics model
cd scope && pytest                               # 127 unit tests
python -m sonarscope.cli selfcheck               # the oscilloscope analyzer checks itself
```

Use your board's COM port (Windows) or `/dev/ttyUSB0` (Linux). With a Siglent, Rigol or Analog
Discovery scope, `sonarscope suite` captures and grades the whole test plan automatically; see
[scope/README.md](scope/README.md).

---

## 6. Serial commands (quick reference)

| Command | Does |
|---|---|
| `HELP` | lists everything |
| `ENV turbidity=40 range=120` | set environment values (also `temp`, `salinity`, `depth`); `ENV AUTO` hands back to the pots |
| `MOD auto` / `lfm` / `geometric` / `barker13` / `cw` | force a modulation, or let the model choose |
| `WIN auto` / `rect` / `tukey` / `hann` / `hamming` / `blackman` | force a window, or let the model choose |
| `PRESET <name>` | fixed test pulse; `DEMO` returns to adaptive mode |
| `STATUS` | what is on air right now |
| `IDLE` / `RUN` | stop / start pinging |

The on-board **BOOT** button also works: a short press changes modulation, a long press changes the window.
Full list: [firmware/README.md](firmware/README.md).

## 7. Measured results

| | Result |
|---|---|
| DAC codes vs reference model (14 test pulses) | 13 identical; 1 differs by one step on 2 of 10 000 samples |
| Adaptation decisions vs reference model (105 environments) | 105 match |
| DMA streaming | 0 missed buffers over about 890 pings; 0 ping-timing jitter |
| Time to compute a new pulse | 1.5 ms (1 ms pulse) to 8.2 ms (5 ms pulse) |
| Flash / RAM used on the ESP32 | 25 % / 34 % |
| Filter design (simulated) | −0.9 dB at 500 kHz, about −42 dB at the 1.5 MHz image |

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| No COM port appears | use a data USB cable; install the CP210x (or CH340) driver |
| Upload fails with "Access is denied" | close the Serial Monitor or dashboard: they hold the port |
| Nothing on the scope, or a slow 50 Hz wave | clip onto **G25** (not G26); set the trigger to Normal; what you see is mains hum picked up by a floating lead |
| Serial Monitor shows garbage | set it to 115200 baud |
| The pulse keeps changing on its own | an input pin is floating: wire the pot, tie the pin to GND, or set its `*_WIRED` flag to 0 |
| The board resets when the dashboard connects | normal for ESP32 DevKits; the dashboard waits for it to boot |
| Waveform jagged at 500 kHz | normal at the bare DAC; measure after the filter board |

## 9. Glossary

| Term | Meaning |
|---|---|
| DAC | digital-to-analog converter: turns numbers into a voltage (here 8-bit, 256 steps, on GPIO25) |
| DMA | direct memory access: hardware that moves samples to the DAC without the CPU |
| MSPS | million samples per second; the DAC runs at 2 MSPS |
| NTU | nephelometric turbidity units: how cloudy the water is |
| FFT | the frequency content of a signal; the scope's MATH → FFT shows it |
| Matched filter / pulse compression | correlating the echo with the transmitted pulse, which squeezes a long chirp into a sharp peak |
| Sidelobe | a smaller false peak next to the real one after compression |
| Range resolution | the closest two targets can be and still appear separate: sound speed ÷ (2 × bandwidth) |
| Blind zone | the distance covered while the pulse is still being sent: the sonar cannot hear echoes from inside it |
| Ping interval (PRI) | time between pings |

## 10. References

* R. J. Urick, *Principles of Underwater Sound*, 3rd ed., McGraw-Hill, 1983.
* K. V. Mackenzie, "Nine-term equation for sound speed in the oceans," *J. Acoust. Soc. Am.* 70(3), 1981.
* R. E. Francois and G. R. Garrison, "Sound absorption based on ocean measurements," *J. Acoust. Soc. Am.* 72(3) and 72(6), 1982.
* A. S. Richards, A. D. Heathershaw and P. D. Thorne, "The effect of suspended particulate matter on sound attenuation in seawater," *J. Acoust. Soc. Am.* 100(3), 1996.
* C. E. Cook and M. Bernfeld, *Radar Signals*, Academic Press, 1967.
* R. H. Barker, "Group synchronizing of binary digital systems," in *Communication Theory*, Butterworths, 1953.
* F. J. Harris, "On the use of windows for harmonic analysis with the discrete Fourier transform," *Proc. IEEE* 66(1), 1978.
* J. Karki, "Active Low-Pass Filter Design," Texas Instruments SLOA049.
* Espressif, *ESP-IDF Programming Guide*: DAC continuous mode.
