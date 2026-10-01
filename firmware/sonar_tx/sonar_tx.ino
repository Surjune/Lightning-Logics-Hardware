// Adaptive software-defined sonar transmitter, ESP32 + built-in DAC.
//
// Output: GPIO25 (DAC channel 0), 2 MSPS, fed by DMA. Two ways to drive it:
//
// * Adaptive mode (at power-up): the environment (turbidity, range, temperature, salinity,
//   depth; pots or `ENV` values) goes through a physics model (env_model.cpp: sound speed,
//   seawater + sediment absorption, spreading loss) that picks the band, pulse length,
//   amplitude, window, modulation and ping interval.
//   BOOT button: short press = next modulation (auto, LFM, geometric, Barker-13, CW),
//   long press = next window (auto, rect, tukey, hann, hamming, blackman).
// * Host mode (after CFG / NEXT / PRI / RUN / IDLE / PRESET): the serial test-mode protocol
//   that `sonarscope suite --dut serial` speaks.
//
// Serial commands, one per line, one reply line (OK ... / ERR ...) each:
//   CFG <PulseSpec JSON>   select the pulse; OK once it is on air
//   NEXT <PulseSpec JSON>  change through the adaptation path, T0 marker steps high
//   PRI <seconds>          ping repetition interval
//   RUN | IDLE             start pinging / stop at mid-scale after the current ping
//   PRESET <name>          load a named sonarscope preset (lfm_hi, barker13, ...)
//   ENV [k=v ...|AUTO]     set environment inputs (turbidity, range, temp, salinity, depth);
//                          AUTO returns them to the pots / defaults; no argument lists them
//   MOD <mode> | WIN <mode>  modulation / window for adaptive mode ("auto" = the model picks)
//   DEMO                   back to adaptive mode
//   TELEM ON|OFF           '@{json}' status lines every 200 ms and on every change
//   STATUS | DUMP | HELP   state / DAC codes of the active pulse (hex) / command list
// Other lines start with '#' (log) or '@' (telemetry).
//
// Arduino IDE: board "ESP32 Dev Module", esp32 core 3.x, Serial Monitor at 115200 baud
// with "Newline" line endings.

#include <Arduino.h>
#include <stdarg.h>

#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "Select an ESP32 board (ESP32 Dev Module): this firmware needs the ESP32's built-in DAC."
#endif
#if ESP_ARDUINO_VERSION_MAJOR < 3
#error "Needs the esp32 Arduino core 3.x (Boards Manager: 'esp32 by Espressif Systems')."
#endif

#include "config.h"
#include "dac_stream.h"
#include "env_model.h"
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

// ---- environment inputs ---------------------------------------------------------------------
constexpr int ENV_STEPS = 50;  // pot resolution: 2 % steps, with hysteresis

struct EnvInput {
  const char *key;
  const char *unit;
  int pin;
  bool wired;
  double lo, hi, def;
  bool logScale;
  int step;         // last pot reading, -1 = none yet
  bool overridden;  // set with ENV
  double value;     // the ENV value
};

EnvInput INPUTS[] = {
    {"turbidity", "NTU", PIN_POT_TURBIDITY, USE_POTS && TURBIDITY_POT_WIRED, 0.0, NTU_MAX, 0.0, false, -1, false, 0.0},
    {"range", "m", PIN_POT_RANGE, USE_POTS && RANGE_POT_WIRED, RANGE_MIN_M, RANGE_MAX_M, 60.0, true, -1, false, 0.0},
    {"temp", "C", PIN_POT_TEMP, USE_POTS && TEMP_POT_WIRED, 0.0, 35.0, 25.0, false, -1, false, 0.0},
    {"salinity", "PSU", PIN_POT_SALINITY, USE_POTS && SALINITY_POT_WIRED, 0.0, 40.0, 35.0, false, -1, false, 0.0},
    {"depth", "m", PIN_POT_DEPTH, USE_POTS && DEPTH_POT_WIRED, 0.0, 300.0, 10.0, false, -1, false, 0.0},
};
constexpr int N_INPUTS = sizeof(INPUTS) / sizeof(INPUTS[0]);

double inputValue(const EnvInput &in) {
  if (in.overridden) return in.value;
  if (!in.wired || in.step < 0) return in.def;
  const double x = in.step / (double)ENV_STEPS;
  return in.logScale ? in.lo * pow(in.hi / in.lo, x) : in.lo + (in.hi - in.lo) * x;
}

const char *inputSource(const EnvInput &in) { return in.overridden ? "set" : (in.wired ? "pot" : "default"); }

Environment currentEnv() {
  Environment e;
  e.turbidity_ntu = inputValue(INPUTS[0]);
  e.range_m = inputValue(INPUTS[1]);
  e.temp_c = inputValue(INPUTS[2]);
  e.salinity_psu = inputValue(INPUTS[3]);
  e.depth_m = inputValue(INPUTS[4]);
  return e;
}

// ---- state -----------------------------------------------------------------------------------
PulseSpec current;           // last pulse handed to the stream
bool hostMode = false;       // true: serial protocol in control, environment ignored
uint32_t synthUs = 0;        // time to synthesise the last pulse
uint32_t markerOffAt = 0;
uint32_t seq = 0;            // bumps on every pulse / PRI change (the dashboard re-reads DUMP)

ModMode modMode = ModMode::AUTO;
WinMode winMode = WinMode::AUTO;
Decision decision;
bool haveDecision = false;
bool envDirty = true;
bool forceReload = true;
uint32_t lastAdcMs = 0;

bool telemetryOn = false;
uint32_t lastTelemetryMs = 0;
bool telemetryDue = false;

// ---- output helpers ----------------------------------------------------------------------------
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

bool sameSpec(const PulseSpec &a, const PulseSpec &b) {
  return a.kind == b.kind && a.f0 == b.f0 && a.f1 == b.f1 && a.duration == b.duration && a.window == b.window &&
         a.tukey_alpha == b.tukey_alpha && a.amplitude == b.amplitude && a.chip == b.chip &&
         a.rc_shaping == b.rc_shaping;
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
  ++seq;
  telemetryDue = true;
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

// ---- telemetry ----------------------------------------------------------------------------------
struct Out {  // bounded string builder
  char buf[1600];
  size_t n = 0;
  void add(const char *fmt, ...) {
    if (n >= sizeof buf) return;
    va_list ap;
    va_start(ap, fmt);
    const int k = vsnprintf(buf + n, sizeof buf - n, fmt, ap);
    va_end(ap);
    if (k > 0) n = n + (size_t)k < sizeof buf ? n + (size_t)k : sizeof buf - 1;
  }
};

void sendTelemetry() {
  static Out o;
  o.n = 0;
  uint32_t samples = 0;
  streamActive(&samples);
  o.add("@{\"v\":1,\"seq\":%lu,\"mode\":\"%s\",\"run\":%d,\"mod\":\"%s\",\"win\":\"%s\",\"fs\":%lu,\"env\":{",
        (unsigned long)seq, hostMode ? "host" : "adaptive", streamRunning() ? 1 : 0, modName(modMode),
        winModeName(winMode), (unsigned long)FS_DAC);
  for (int i = 0; i < N_INPUTS; ++i) o.add("%s\"%s\":%.2f", i ? "," : "", INPUTS[i].key, inputValue(INPUTS[i]));
  o.add("},\"src\":{");
  for (int i = 0; i < N_INPUTS; ++i) o.add("%s\"%s\":\"%s\"", i ? "," : "", INPUTS[i].key, inputSource(INPUTS[i]));
  const PulseSpec &s = current;
  // full precision (%.17g round-trips a double), so a receiver rebuilds exactly this pulse
  o.add("},\"pulse\":{\"kind\":\"%s\",\"f0\":%.17g,\"f1\":%.17g,\"duration\":%.17g,\"window\":\"%s\","
        "\"tukey_alpha\":%.17g,\"amplitude\":%.17g,\"chip\":%.17g,\"rc_shaping\":%.17g,\"samples\":%lu},\"pri\":%.6f",
        kindName(s.kind), s.f0, s.f1, s.kind == PulseKind::BARKER13 ? 13 * s.chip : s.duration, windowName(s.window),
        s.tukey_alpha, s.amplitude, s.chip, s.rc_shaping, (unsigned long)samples,
        streamPri() / (double)FS_DAC);
  if (!hostMode && haveDecision) {
    const Decision &d = decision;
    o.add(",\"phys\":{\"c\":%.2f,\"fc\":%.0f,\"bw\":%.0f,\"alpha\":%.2f,\"abs2w\":%.2f,\"tl\":%.2f,"
          "\"demand\":%.4f,\"limited\":%d,\"reach\":%.1f,\"res\":%.5f,\"blind\":%.4f,\"tb\":%.2f,"
          "\"energy_db\":%.2f,\"avg_power_db\":%.2f}",
          d.sound_speed, d.fc_hz, d.bandwidth_hz, d.alpha_db_km, d.absorption_2way_db, d.tl_2way_db, d.demand,
          d.range_limited ? 1 : 0, d.achievable_range_m, d.range_resolution_m, d.blind_zone_m, d.time_bandwidth,
          d.energy_db, d.avg_power_db);
  }
  o.add(",\"stats\":{\"pings\":%lu,\"synth_us\":%lu,\"underruns\":%lu}}\n", (unsigned long)streamPings(),
        (unsigned long)synthUs, (unsigned long)streamUnderruns());
  Serial.write((const uint8_t *)o.buf, o.n);
  lastTelemetryMs = millis();
  telemetryDue = false;
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

String envSummary() {
  String s;
  char buf[48];
  for (int i = 0; i < N_INPUTS; ++i) {
    snprintf(buf, sizeof buf, "%s%s=%.2f", i ? " " : "", INPUTS[i].key, inputValue(INPUTS[i]));
    s += buf;
  }
  return s;
}

void replyStatus() {
  uint32_t n = 0;
  streamActive(&n);
  char buf[480];
  snprintf(buf, sizeof buf,
           "mode=%s run=%d kind=%s f0=%.1f f1=%.1f duration=%.6f window=%s tukey_alpha=%.3f "
           "amplitude=%.3f chip=%.7f rc_shaping=%.3f samples=%lu pri=%.6f pings=%lu synth_us=%lu "
           "underruns=%lu dma_samples=%lu mod=%s win=%s ",
           hostMode ? "host" : "adaptive", streamRunning() ? 1 : 0, kindName(current.kind), current.f0,
           current.f1, pulseSamples(current, FS_DAC) / (double)FS_DAC, windowName(current.window),
           current.tukey_alpha, current.amplitude, current.chip, current.rc_shaping, (unsigned long)n,
           streamPri() / (double)FS_DAC, (unsigned long)streamPings(), (unsigned long)synthUs,
           (unsigned long)streamUnderruns(), (unsigned long)streamSamplesPerBuffer(), modName(modMode),
           winModeName(winMode));
  reply("OK", (String(buf) + envSummary()).c_str());
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

void enterAdaptiveMode() {
  hostMode = false;
  for (EnvInput &in : INPUTS) in.step = -1;  // re-read the pots
  envDirty = true;
  forceReload = true;
  streamRun(true);
}

// ENV turbidity=40 range=120 ... | ENV AUTO | ENV
bool handleEnv(char *arg, const char *&err) {
  if (!strcasecmp(arg, "AUTO") || !strcasecmp(arg, "CLEAR")) {
    for (EnvInput &in : INPUTS) in.overridden = false;
    return true;
  }
  double values[N_INPUTS];
  bool set[N_INPUTS] = {};
  for (char *tok = strtok(arg, " \t"); tok; tok = strtok(nullptr, " \t")) {
    char *eq = strchr(tok, '=');
    if (!eq) {
      err = "expected key=value";
      return false;
    }
    *eq = '\0';
    int i = 0;
    while (i < N_INPUTS && strcasecmp(tok, INPUTS[i].key)) ++i;
    if (i == N_INPUTS) {
      err = "keys: turbidity, range, temp, salinity, depth";
      return false;
    }
    char *end = nullptr;
    const double v = strtod(eq + 1, &end);
    if (end == eq + 1 || v < INPUTS[i].lo || v > INPUTS[i].hi) {
      static char msg[64];
      snprintf(msg, sizeof msg, "%s must be %g to %g %s", INPUTS[i].key, INPUTS[i].lo, INPUTS[i].hi, INPUTS[i].unit);
      err = msg;
      return false;
    }
    values[i] = v;
    set[i] = true;
  }
  for (int i = 0; i < N_INPUTS; ++i) {  // all or nothing
    if (set[i]) {
      INPUTS[i].overridden = true;
      INPUTS[i].value = values[i];
    }
  }
  return true;
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
    hostMode = true;
    if (next) raiseMarker();
    if (!loadPulse(spec, !next, err)) return reply("ERR", err);
    reply("OK");
  } else if (!strcasecmp(line, "PRESET")) {
    for (const Preset &p : PRESETS) {
      if (!strcasecmp(arg, p.name)) {
        hostMode = true;
        if (!loadPulse(p.spec, true, err)) return reply("ERR", err);
        return reply("OK", describe(p.spec).c_str());
      }
    }
    reply("ERR", "unknown preset; HELP lists them");
  } else if (!strcasecmp(line, "PRI")) {
    char *end = nullptr;
    const double pri = strtod(arg, &end);
    if (end == arg || !(pri >= 1e-4 && pri <= 10.0)) return reply("ERR", "PRI must be 0.0001 to 10 s");
    hostMode = true;
    streamSetPri((uint32_t)(pri * FS_DAC + 0.5));
    ++seq;
    reply("OK");
  } else if (!strcasecmp(line, "RUN")) {
    hostMode = true;
    streamRun(true);
    reply("OK");
  } else if (!strcasecmp(line, "IDLE")) {
    hostMode = true;
    streamRun(false);
    reply("OK");
  } else if (!strcasecmp(line, "ENV")) {
    if (!*arg) return reply("OK", envSummary().c_str());
    if (!handleEnv(arg, err)) return reply("ERR", err);
    if (hostMode) enterAdaptiveMode();
    envDirty = true;
    reply("OK");
  } else if (!strcasecmp(line, "MOD") || !strcasecmp(line, "WIN")) {
    const bool mod = !strcasecmp(line, "MOD");
    if (mod ? !parseModMode(arg, modMode) : !parseWinMode(arg, winMode))
      return reply("ERR", mod ? "MOD auto|lfm|geometric|barker13|cw" : "WIN auto|rect|tukey|hann|hamming|blackman");
    if (hostMode) enterAdaptiveMode();
    envDirty = true;
    reply("OK");
  } else if (!strcasecmp(line, "DEMO") || !strcasecmp(line, "ADAPT")) {
    enterAdaptiveMode();
    reply("OK");
  } else if (!strcasecmp(line, "TELEM")) {
    if (!strcasecmp(arg, "ON")) telemetryOn = true;
    else if (!strcasecmp(arg, "OFF")) telemetryOn = false;
    else return reply("ERR", "TELEM ON|OFF");
    telemetryDue = telemetryOn;
    reply("OK");
  } else if (!strcasecmp(line, "STATUS")) {
    replyStatus();
  } else if (!strcasecmp(line, "DUMP")) {
    replyDump();
  } else if (!strcasecmp(line, "HELP")) {
    String s = "commands: CFG <json> | NEXT <json> | PRI <s> | RUN | IDLE | PRESET <name> | ENV [k=v..|AUTO] | "
               "MOD <m> | WIN <w> | DEMO | TELEM ON|OFF | STATUS | DUMP | HELP; presets:";
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

// ---- adaptive mode ----------------------------------------------------------------------------
int readStep(int pin, int current) {
  uint32_t sum = 0;
  for (int i = 0; i < 8; ++i) sum += analogRead(pin);
  const float pos = sum / (8 * 4095.0f) * ENV_STEPS;
  if (current >= 0 && fabsf(pos - current) < 0.75f) return current;  // hysteresis
  return (int)lroundf(pos);
}

void pollEnvironment() {
  if (millis() - lastAdcMs < ADC_PERIOD_MS) return;
  lastAdcMs = millis();
  for (EnvInput &in : INPUTS) {
    if (!in.wired) continue;
    const int s = readStep(in.pin, in.step);
    if (s != in.step) {
      in.step = s;
      if (!in.overridden) envDirty = true;
    }
  }
  if (!envDirty || streamSwapPending()) return;  // retry after the pending swap

  const Environment env = currentEnv();
  decision = decide(env, modMode, winMode);
  haveDecision = true;
  envDirty = false;
  telemetryDue = true;
  const uint32_t priSamples = (uint32_t)(decision.pri_s * FS_DAC + 0.5);
  if (!forceReload && sameSpec(decision.spec, current) && priSamples == streamPri()) return;

  forceReload = false;
  raiseMarker();
  streamSetPri(priSamples);
  const char *err = nullptr;
  if (!loadPulse(decision.spec, false, err)) {
    Serial.printf("# adaptive pulse rejected: %s\n", err);
    return;
  }
  Serial.printf("# ENV %.0f NTU, %.0f m, %.1f C, %.1f PSU, %.0f m deep -> %s, PRI %.1f ms | res %.1f cm%s | synth %.2f ms\n",
                env.turbidity_ntu, env.range_m, env.temp_c, env.salinity_psu, env.depth_m,
                describe(decision.spec).c_str(), decision.pri_s * 1e3, decision.range_resolution_m * 100,
                decision.range_limited ? " | range-limited" : "", synthUs / 1000.0);
}

void onButton(bool longPress) {
  if (hostMode) {
    Serial.println("# adaptive mode");
    enterAdaptiveMode();
    return;
  }
  if (longPress) {
    winMode = (WinMode)(((int)winMode + 1) % 6);
    Serial.printf("# window: %s\n", winModeName(winMode));
  } else {
    modMode = (ModMode)(((int)modMode + 1) % 5);
    Serial.printf("# modulation: %s\n", modName(modMode));
  }
  envDirty = true;
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
  Serial.setTxBufferSize(4096);  // telemetry lines queue without stalling the loop
  Serial.begin(SERIAL_BAUD);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_MARKER, OUTPUT);
  digitalWrite(PIN_MARKER, LOW);
  analogReadResolution(12);

  pulseInit();
  envModelInit();
  if (const char *err = streamBegin(FS_DAC)) {
    for (;;) {
      Serial.printf("# FATAL: DAC stream failed to start: %s\n", err);
      delay(2000);
    }
  }
  Serial.println("# sonar_tx ready: GPIO25 DAC out @ 2 MSPS, T0 marker on GPIO27");
  // list only the pots that config.h enables, so the log matches the wiring
  Serial.print("# adaptive mode, pots:");
  int pots = 0;
  for (const EnvInput &in : INPUTS) {
    if (!in.wired) continue;
    Serial.printf("%s %s GPIO%d", pots++ ? "," : "", in.key, in.pin);
  }
  Serial.println(pots ? " (the rest: defaults or ENV, see config.h); BOOT = next modulation, hold = next window"
                      : " none (all inputs: defaults or ENV, see config.h); BOOT = next modulation, hold = next window");
  Serial.println("# type HELP for the serial commands");
  enterAdaptiveMode();
}

void loop() {
  pollSerial();
  pollButton();
  if (!hostMode) pollEnvironment();
  if (markerOffAt && (int32_t)(millis() - markerOffAt) >= 0) {
    digitalWrite(PIN_MARKER, LOW);
    markerOffAt = 0;
  }
  if (telemetryOn && (telemetryDue || millis() - lastTelemetryMs >= TELEMETRY_PERIOD_MS)) sendTelemetry();
  delay(1);  // lets the idle task run (CPU waits for interrupts between polls)
}
