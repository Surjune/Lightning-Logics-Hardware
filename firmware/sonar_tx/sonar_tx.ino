// Adaptive software-defined sonar transmitter (PS 26058), ESP32 + built-in DAC.
//
// Output: GPIO25 (DAC channel 0), 2 MSPS, fed by DMA. Two ways to drive it:
//
// * Demo mode (at power-up): two pots stand in for the environment sensors and the
//   firmware adapts centre frequency / bandwidth, pulse duration and amplitude to them.
//   BOOT button: short press = next modulation (LFM, geometric, Barker-13, CW),
//   long press = next window (auto, rect, tukey, hann, hamming, blackman).
// * Host mode (after any protocol command): the serial test-mode protocol that
//   `sonarscope suite --dut serial` speaks. One command per line, one reply per command:
//     CFG <PulseSpec JSON>   select the pulse; OK once it is on air
//     NEXT <PulseSpec JSON>  change through the adaptation path, T0 marker steps high
//     PRI <seconds>          ping repetition interval
//     RUN | IDLE             start pinging / stop at mid-scale after the current ping
//     PRESET <name>          load a named sonarscope preset (lfm_hi, barker13, ...)
//     DEMO                   back to pot control
//     STATUS | DUMP | HELP   state / DAC codes of the active pulse (hex) / command list
//   Replies start with OK or ERR. Other lines this firmware prints start with '#'.
//
// Arduino IDE: board "ESP32 Dev Module", esp32 core 3.x, Serial Monitor at 115200 baud
// with "Newline" line endings.

#include <Arduino.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Select an ESP32 board (ESP32 Dev Module): this firmware needs the ESP32's built-in DAC."
#endif
#if ESP_ARDUINO_VERSION_MAJOR < 3
#error "Needs the esp32 Arduino core 3.x (Boards Manager: 'esp32 by Espressif Systems')."
#endif

#include "config.h"
#include "dac_stream.h"
#include "pulse.h"

namespace {

// ---- named pulses, as scope/src/sonarscope/waveforms.py preset() defines them ----------
struct Preset {
  const char *name;
  PulseSpec spec;
};

PulseSpec makeSpec(PulseKind kind, double f0, double f1, double duration, Window window,
                   double alpha = 0.2, double amplitude = 1.0, double chip = 40e-6, double rc = 0.0) {
  PulseSpec s;
  s.kind = kind;
  s.f0 = f0;
  s.f1 = f1;
  s.duration = duration;
  s.window = window;
  s.tukey_alpha = alpha;
  s.amplitude = amplitude;
  s.chip = chip;
  s.rc_shaping = rc;
  return s;
}

const Preset PRESETS[] = {
    {"lfm_hi", makeSpec(PulseKind::LFM, 400e3, 500e3, 2e-3, Window::TUKEY)},
    {"lfm_hi_hann", makeSpec(PulseKind::LFM, 400e3, 500e3, 2e-3, Window::HANN)},
    {"lfm_hi_rect", makeSpec(PulseKind::LFM, 400e3, 500e3, 2e-3, Window::RECT)},
    {"lfm_lo", makeSpec(PulseKind::LFM, 100e3, 140e3, 5e-3, Window::TUKEY)},
    {"lfm_down", makeSpec(PulseKind::LFM, 500e3, 400e3, 2e-3, Window::TUKEY)},
    {"lfm_full", makeSpec(PulseKind::LFM, 100e3, 500e3, 2e-3, Window::TUKEY)},
    {"geometric", makeSpec(PulseKind::GEOMETRIC, 100e3, 500e3, 2e-3, Window::TUKEY)},
    {"barker13", makeSpec(PulseKind::BARKER13, 300e3, 300e3, 13 * 40e-6, Window::TUKEY, 0.12, 1.0, 40e-6, 0.2)},
    {"tone_101k", makeSpec(PulseKind::CW, 101.3e3, 101.3e3, 5e-3, Window::TUKEY, 0.1)},
    {"tone_250k", makeSpec(PulseKind::CW, 250e3, 250e3, 5e-3, Window::TUKEY, 0.1)},
    {"tone_487k", makeSpec(PulseKind::CW, 487.3e3, 487.3e3, 5e-3, Window::TUKEY, 0.1)},
    {"tone_500k", makeSpec(PulseKind::CW, 500e3, 500e3, 5e-3, Window::TUKEY, 0.1)},
    {"clear_reef", makeSpec(PulseKind::LFM, 400e3, 500e3, 1e-3, Window::HANN, 0.2, 0.5)},
    {"muddy_estuary", makeSpec(PulseKind::LFM, 100e3, 140e3, 5e-3, Window::TUKEY, 0.2, 1.0)},
};

// ---- state -----------------------------------------------------------------------------
PulseSpec current;           // last pulse handed to the stream
bool hostMode = false;       // true: serial protocol in control, pots ignored
uint32_t synthUs = 0;        // time to synthesise the last pulse
uint32_t markerOffAt = 0;

// demo mode
constexpr int ENV_STEPS = 50;  // pot resolution: 2 % steps, with hysteresis
const PulseKind DEMO_KINDS[] = {PulseKind::LFM, PulseKind::GEOMETRIC, PulseKind::BARKER13, PulseKind::CW};
int demoKind = 0;
int demoWindow = 0;            // 0 = auto, otherwise 1 + Window value
int turbStep = -1, reachStep = -1;
bool demoDirty = true;
uint32_t lastAdcMs = 0;

// ---- output helpers ----------------------------------------------------------------------
void raiseMarker() {
  if (digitalRead(PIN_MARKER)) {  // already high from a recent change: make a fresh edge
    digitalWrite(PIN_MARKER, LOW);
    delayMicroseconds(20);
  }
  digitalWrite(PIN_MARKER, HIGH);
  markerOffAt = millis() + MARKER_HOLD_MS;
  if (markerOffAt == 0) markerOffAt = 1;
}

String describe(const PulseSpec &s) {
  char buf[160];
  const double ms = pulseSamples(s, FS_DAC) * 1e3 / FS_DAC;
  if (s.kind == PulseKind::LFM || s.kind == PulseKind::GEOMETRIC) {
    snprintf(buf, sizeof buf, "%s %.1f-%.1f kHz, %.2f ms, amp %.2f, %s", kindName(s.kind), s.f0 / 1e3,
             s.f1 / 1e3, ms, s.amplitude, windowName(s.window));
  } else if (s.kind == PulseKind::BARKER13) {
    snprintf(buf, sizeof buf, "barker13 on %.1f kHz, chip %.1f us, %.2f ms, amp %.2f, %s", s.f0 / 1e3,
             s.chip * 1e6, ms, s.amplitude, windowName(s.window));
  } else {
    snprintf(buf, sizeof buf, "cw %.1f kHz, %.2f ms, amp %.2f, %s", s.f0 / 1e3, ms, s.amplitude,
             windowName(s.window));
  }
  return String(buf);
}

uint32_t frameTimeoutMs() {
  const uint64_t samples = (uint64_t)streamPri() + pulseSamples(current, FS_DAC);
  return (uint32_t)(samples * 1000 / FS_DAC) + 200;
}

// Synthesises `spec` into the back buffer and hands it to the stream. With waitLive the
// call returns once the pulse is on air (at the next ping boundary).
bool loadPulse(const PulseSpec &spec, bool waitLive, const char *&err) {
  err = pulseValidate(spec, FS_DAC, MAX_PULSE_SAMPLES);
  if (err) return false;
  if (!streamWaitSwap(frameTimeoutMs())) {
    err = "previous change still pending";
    return false;
  }
  const uint32_t t0 = micros();
  const uint32_t n = pulseSynthesize(spec, FS_DAC, streamBackBuffer());
  synthUs = micros() - t0;
  streamCommit(n);
  current = spec;
  if (waitLive && !streamWaitSwap(frameTimeoutMs())) {
    err = "pulse not swapped in: DMA stream stalled";
    return false;
  }
  return true;
}

// ---- minimal JSON field reader (flat objects from json.dumps) ------------------------------
enum class Field { Absent, Ok, Bad };

const char *jsonValue(const char *js, const char *key) {
  char pattern[32];
  snprintf(pattern, sizeof pattern, "\"%s\"", key);
  for (const char *p = strstr(js, pattern); p; p = strstr(p + 1, pattern)) {
    const char *q = p + strlen(pattern);
    while (*q == ' ' || *q == '\t') ++q;
    if (*q != ':') continue;
    ++q;
    while (*q == ' ' || *q == '\t') ++q;
    return q;
  }
  return nullptr;
}

Field jsonNumber(const char *js, const char *key, double &out) {
  const char *v = jsonValue(js, key);
  if (!v) return Field::Absent;
  char *end = nullptr;
  const double d = strtod(v, &end);
  if (end == v) return Field::Bad;
  out = d;
  return Field::Ok;
}

Field jsonString(const char *js, const char *key, char *out, size_t size) {
  const char *v = jsonValue(js, key);
  if (!v) return Field::Absent;
  if (*v++ != '"') return Field::Bad;
  size_t i = 0;
  while (*v && *v != '"' && i + 1 < size) out[i++] = *v++;
  out[i] = '\0';
  return *v == '"' ? Field::Ok : Field::Bad;
}

// Fields missing from the JSON keep the PulseSpec defaults, as PulseSpec.from_dict does.
bool parseSpec(const char *js, PulseSpec &spec, const char *&err) {
  spec = PulseSpec();
  if (!strchr(js, '{')) {
    err = "expected a PulseSpec JSON object";
    return false;
  }
  char word[16];
  Field f = jsonString(js, "kind", word, sizeof word);
  if (f == Field::Bad || (f == Field::Ok && !parseKind(word, spec.kind))) {
    err = "kind must be lfm, geometric, barker13 or cw";
    return false;
  }
  f = jsonString(js, "window", word, sizeof word);
  if (f == Field::Bad || (f == Field::Ok && !parseWindow(word, spec.window))) {
    err = "window must be rect, tukey, hann, hamming or blackman";
    return false;
  }
  struct {
    const char *key;
    double *dst;
  } numbers[] = {{"f0", &spec.f0}, {"f1", &spec.f1}, {"duration", &spec.duration},
                 {"tukey_alpha", &spec.tukey_alpha}, {"amplitude", &spec.amplitude},
                 {"chip", &spec.chip}, {"rc_shaping", &spec.rc_shaping}};
  for (auto &n : numbers) {
    if (jsonNumber(js, n.key, *n.dst) == Field::Bad) {
      static char msg[48];
      snprintf(msg, sizeof msg, "bad number for %s", n.key);
      err = msg;
      return false;
    }
  }
  return true;
}

// ---- serial protocol ---------------------------------------------------------------------
void reply(const char *status, const char *detail = nullptr) {
  Serial.print(status);
  if (detail && *detail) {
    Serial.print(' ');
    Serial.print(detail);
  }
  Serial.print('\n');
}

void replyStatus() {
  uint32_t n = 0;
  streamActive(&n);
  char buf[400];
  snprintf(buf, sizeof buf,
           "mode=%s run=%d kind=%s f0=%.1f f1=%.1f duration=%.6f window=%s tukey_alpha=%.3f "
           "amplitude=%.3f chip=%.7f rc_shaping=%.3f samples=%lu pri=%.6f pings=%lu synth_us=%lu "
           "underruns=%lu dma_samples=%lu turbidity=%.2f reach=%.2f",
           hostMode ? "host" : "demo", streamRunning() ? 1 : 0, kindName(current.kind), current.f0,
           current.f1, pulseSamples(current, FS_DAC) / (double)FS_DAC, windowName(current.window),
           current.tukey_alpha, current.amplitude, current.chip, current.rc_shaping, (unsigned long)n,
           streamPri() / (double)FS_DAC, (unsigned long)streamPings(), (unsigned long)synthUs,
           (unsigned long)streamUnderruns(), (unsigned long)streamSamplesPerBuffer(),
           turbStep < 0 ? -1.0 : turbStep / (double)ENV_STEPS, reachStep < 0 ? -1.0 : reachStep / (double)ENV_STEPS);
  reply("OK", buf);
}

void replyDump() {
  if (!streamWaitSwap(frameTimeoutMs())) {
    reply("ERR", "change still pending");
    return;
  }
  uint32_t n = 0;
  const uint8_t *codes = streamActive(&n);
  static const char HEX_DIGITS[] = "0123456789abcdef";
  char chunk[257];
  Serial.print("OK ");
  Serial.print(n);
  Serial.print(' ');
  for (uint32_t i = 0; i < n;) {
    size_t k = 0;
    for (; k < 256 && i < n; ++i) {
      chunk[k++] = HEX_DIGITS[codes[i] >> 4];
      chunk[k++] = HEX_DIGITS[codes[i] & 15];
    }
    chunk[k] = '\0';
    Serial.print(chunk);
  }
  Serial.print('\n');
}

void enterHostMode() {
  if (!hostMode) hostMode = true;
}

void enterDemoMode() {
  hostMode = false;
  turbStep = reachStep = -1;  // re-read the pots and re-synthesise
  demoDirty = true;
  streamSetPri((uint32_t)(DEFAULT_PRI_S * FS_DAC + 0.5));
  streamRun(true);
}

void handleCommand(char *line) {
  while (*line == ' ' || *line == '\t') ++line;
  char *arg = line;
  while (*arg && *arg != ' ' && *arg != '\t') ++arg;
  if (*arg) *arg++ = '\0';
  while (*arg == ' ' || *arg == '\t') ++arg;
  const char *err = nullptr;

  if (!strcasecmp(line, "CFG") || !strcasecmp(line, "NEXT")) {
    const bool next = !strcasecmp(line, "NEXT");
    PulseSpec spec;
    if (!parseSpec(arg, spec, err)) return reply("ERR", err);
    enterHostMode();
    if (next) raiseMarker();
    if (!loadPulse(spec, !next, err)) return reply("ERR", err);
    reply("OK");
  } else if (!strcasecmp(line, "PRESET")) {
    for (const Preset &p : PRESETS) {
      if (!strcasecmp(arg, p.name)) {
        enterHostMode();
        if (!loadPulse(p.spec, true, err)) return reply("ERR", err);
        return reply("OK", describe(p.spec).c_str());
      }
    }
    reply("ERR", "unknown preset; HELP lists them");
  } else if (!strcasecmp(line, "PRI")) {
    char *end = nullptr;
    const double pri = strtod(arg, &end);
    if (end == arg || !(pri >= 1e-4 && pri <= 10.0)) return reply("ERR", "PRI must be 0.0001 to 10 s");
    enterHostMode();
    streamSetPri((uint32_t)(pri * FS_DAC + 0.5));
    reply("OK");
  } else if (!strcasecmp(line, "RUN")) {
    enterHostMode();
    streamRun(true);
    reply("OK");
  } else if (!strcasecmp(line, "IDLE")) {
    enterHostMode();
    streamRun(false);
    reply("OK");
  } else if (!strcasecmp(line, "DEMO")) {
    enterDemoMode();
    reply("OK");
  } else if (!strcasecmp(line, "STATUS")) {
    replyStatus();
  } else if (!strcasecmp(line, "DUMP")) {
    replyDump();
  } else if (!strcasecmp(line, "HELP")) {
    String s = "commands: CFG <json> | NEXT <json> | PRI <s> | RUN | IDLE | PRESET <name> | DEMO | "
               "STATUS | DUMP | HELP; presets:";
    for (const Preset &p : PRESETS) s += String(' ') + p.name;
    reply("OK", s.c_str());
  } else if (*line) {
    reply("ERR", "unknown command; try HELP");
  }
}

void pollSerial() {
  static char line[1024];
  static size_t len = 0;
  static bool overflow = false;
  while (Serial.available()) {
    const int c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[len] = '\0';
      if (overflow) reply("ERR", "line too long");
      else if (len) handleCommand(line);
      len = 0;
      overflow = false;
    } else if (len + 1 < sizeof line) {
      line[len++] = (char)c;
    } else {
      overflow = true;
    }
  }
}

// ---- demo mode: environment -> pulse --------------------------------------------------------
// Turbidity sets the band: suspended sediment scatters high frequencies, so the carrier
// moves from 450 kHz (clear, fine resolution) down to 120 kHz (muddy, penetration), and the
// swept bandwidth scales with it (400-500 kHz down to 100-140 kHz, the sonarscope
// clear_reef / muddy_estuary presets). The energy demand is the larger of turbidity (loss)
// and required reach; it stretches the pulse from 1 to 5 ms and raises the amplitude from
// 0.5 to 1.0, so a clear, close-range scene spends the least battery.
PulseSpec demoSpec(float turbidity, float reach, PulseKind kind, int windowMode) {
  const float energy = max(turbidity, reach);
  const double fc = 450e3 - 330e3 * turbidity;
  const double bw = 100e3 - 60e3 * turbidity;
  PulseSpec s;
  s.kind = kind;
  s.duration = 1e-3 + 4e-3 * energy;
  s.amplitude = 0.5 + 0.5 * energy;
  s.f0 = s.f1 = fc;
  if (kind == PulseKind::LFM || kind == PulseKind::GEOMETRIC) {
    s.f0 = fc - bw / 2;
    s.f1 = fc + bw / 2;
  } else if (kind == PulseKind::BARKER13) {
    s.chip = s.duration / 13;  // longer pulse = longer chips
    s.rc_shaping = 0.2;
  }
  if (windowMode == 0) {  // auto: short pulses favour low sidelobes, long ones energy
    s.window = kind == PulseKind::BARKER13 || energy >= 0.5f ? Window::TUKEY : Window::HANN;
    s.tukey_alpha = kind == PulseKind::BARKER13 ? 0.12 : 0.2;
  } else {
    s.window = (Window)(windowMode - 1);
  }
  return s;
}

const char *zoneName(float turbidity) {
  if (turbidity < 0.33f) return "CLEAR SHALLOW REEF";
  if (turbidity > 0.66f) return "MUDDY ESTUARY";
  return "COASTAL / MIXED";
}

int readStep(int pin, int current) {
#if USE_POTS
  uint32_t sum = 0;
  for (int i = 0; i < 8; ++i) sum += analogRead(pin);
  const float pos = sum / (8 * 4095.0f) * ENV_STEPS;
  if (current >= 0 && fabsf(pos - current) < 0.75f) return current;  // hysteresis
  return (int)lroundf(pos);
#else
  (void)pin;
  return current >= 0 ? current : 0;  // no pots: clear reef, close range
#endif
}

void pollEnvironment() {
  if (millis() - lastAdcMs < ADC_PERIOD_MS) return;
  lastAdcMs = millis();
  const int t = readStep(PIN_POT_TURBIDITY, turbStep);
  const int r = readStep(PIN_POT_REACH, reachStep);
  if (t != turbStep || r != reachStep) {
    turbStep = t;
    reachStep = r;
    demoDirty = true;
  }
  if (!demoDirty || streamSwapPending()) return;  // retry after the pending swap

  const float turbidity = turbStep / (float)ENV_STEPS, reach = reachStep / (float)ENV_STEPS;
  const PulseSpec spec = demoSpec(turbidity, reach, DEMO_KINDS[demoKind], demoWindow);
  const char *err = nullptr;
  raiseMarker();
  if (!loadPulse(spec, false, err)) {
    Serial.printf("# demo pulse rejected: %s\n", err);
  } else {
    Serial.printf("# ENV turbidity %.2f reach %.2f [%s] -> %s | synth %.2f ms\n", turbidity, reach,
                  zoneName(turbidity), describe(spec).c_str(), synthUs / 1000.0);
  }
  demoDirty = false;
}

void onButton(bool longPress) {
  if (hostMode) {
    Serial.println("# demo mode (pots in control)");
    enterDemoMode();
    return;
  }
  if (longPress) {
    demoWindow = (demoWindow + 1) % 6;
    Serial.printf("# window: %s\n", demoWindow ? windowName((Window)(demoWindow - 1)) : "auto");
  } else {
    demoKind = (demoKind + 1) % 4;
    Serial.printf("# modulation: %s\n", kindName(DEMO_KINDS[demoKind]));
  }
  demoDirty = true;
}

void pollButton() {
  static bool down = false;
  static uint32_t since = 0, lastEdge = 0;
  const bool pressed = digitalRead(PIN_BUTTON) == LOW;
  if (pressed == down || millis() - lastEdge < 30) return;  // unchanged or bouncing
  lastEdge = millis();
  down = pressed;
  if (pressed) since = millis();
  else onButton(millis() - since >= LONG_PRESS_MS);
}

}  // namespace

void setup() {
  setCpuFrequencyMhz(CPU_FREQ_MHZ);
  Serial.setRxBufferSize(1024);
  Serial.begin(SERIAL_BAUD);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_MARKER, OUTPUT);
  digitalWrite(PIN_MARKER, LOW);
  analogReadResolution(12);

  pulseInit();
  if (const char *err = streamBegin(FS_DAC)) {
    for (;;) {
      Serial.printf("# FATAL: DAC stream failed to start: %s\n", err);
      delay(2000);
    }
  }
  Serial.println("# sonar_tx ready: GPIO25 DAC out @ 2 MSPS, T0 marker on GPIO27");
  Serial.println("# demo mode: pots GPIO34 (turbidity) / GPIO35 (reach); BOOT = next modulation, hold = next window");
  Serial.println("# type HELP for the serial commands");
  enterDemoMode();
}

void loop() {
  pollSerial();
  pollButton();
  if (!hostMode) pollEnvironment();
  if (markerOffAt && (int32_t)(millis() - markerOffAt) >= 0) {
    digitalWrite(PIN_MARKER, LOW);
    markerOffAt = 0;
  }
  delay(1);  // lets the idle task run (CPU waits for interrupts between polls)
}
