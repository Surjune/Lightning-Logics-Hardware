# Transmitter dashboard

One HTML file, no build step. It connects to the ESP32 over USB with the browser's
Web Serial API and shows, live:

* the environment inputs, with sliders that act as sensors when no pots are wired;
* the decision: band, pulse length, amplitude, window, modulation, ping interval, range
  resolution, blind zone, pulse-compression gain, sound speed, absorption, transmission loss,
  energy per ping and average power;
* the pulse, its spectrum, spectrogram and matched-filter output (plain and with Hamming
  receive weighting, with peak sidelobes). It draws the model of the pulse at once, then reads
  the board's own DAC codes back (`DUMP`) and reports whether they match the model sample for
  sample (on the DevKit: identical, for LFM, geometric, Barker-13 and CW pulses);
* why that band: seawater and sediment attenuation against frequency for the current water,
  the absorption budget over the required range, and the band the model chose.

**Simulate** runs the same physics model and waveform synthesis in the browser, for a demo
without hardware. The simulator's decisions match `sonarscope.acoustics` exactly.

## Running it

Web Serial needs Chrome or Edge on a desktop, and a page served over `https://` or from
`localhost`:

```bash
cd Sonar
python -m http.server 8000
```

Then open <http://localhost:8000/dashboard/>, press **Connect board** and pick the ESP32's
COM port. Close the Arduino Serial Monitor first: only one program can hold the port.

Opening the port resets most ESP32 DevKits (the USB-serial chip's auto-reset), so the
dashboard waits about two seconds for the board to boot before it asks for telemetry.

Opening `index.html` straight from disk works for **Simulate**; whether it can reach the serial
port that way depends on the browser, so use the local server for a board.

The page can also be published with GitHub Pages (Settings → Pages → deploy from `main`), which
serves it over https: the simulator then works for anyone with the link, and **Connect board**
works for anyone with a board.

## Messages it relies on

The dashboard sends `TELEM ON`, `ENV key=value`, `MOD`, `WIN`, `PRESET`, `DEMO` and `DUMP`,
and reads the firmware's `@{json}` telemetry lines (see
[firmware/README.md](../firmware/README.md)). It re-reads the pulse with `DUMP` whenever the
telemetry's `seq` counter changes.
