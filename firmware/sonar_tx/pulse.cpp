// Arduino builds with -Os, which leaves the per-sample helpers as calls; the synthesis
// loop is the one place where speed matters (it bounds the adaptation latency).
#pragma GCC optimize("O2")

#include "pulse.h"

#include <math.h>
#include <strings.h>

#include "config.h"

namespace {

// Direct digital synthesis: the carrier phase is a 64-bit accumulator holding cycles
// in Q16.48, advanced by an integer increment each sample. An LFM chirp only needs the
// increment itself to grow by a constant each sample, so the whole inner loop is
// integer adds plus one interpolated table lookup.
constexpr int LUT_BITS = 12;
constexpr uint32_t LUT_SIZE = 1u << LUT_BITS;
constexpr int FRAC_BITS = 48;
constexpr double ONE_CYCLE = 281474976710656.0;  // 2^48

float sinLut[LUT_SIZE + 1];                // +1 guard entry for interpolation
int8_t chipSeq[MAX_PULSE_SAMPLES];         // Barker-13 chip value (+1/-1) per sample
float rcKernel[MAX_PULSE_SAMPLES / 13 + 2];  // kernel <= one chip <= n/13 samples, + rounding

const int8_t BARKER13[13] = {1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1};

// sin(2*pi*phase) for a Q16.48 phase: 12 index bits and 16 interpolation bits.
inline float sinPhase(uint64_t acc) {
  const uint32_t top = (uint32_t)(acc >> (FRAC_BITS - LUT_BITS - 16));
  const uint32_t i = (top >> 16) & (LUT_SIZE - 1);
  const float f = (float)(top & 0xFFFF) * (1.0f / 65536.0f);
  return sinLut[i] + f * (sinLut[i + 1] - sinLut[i]);
}

// cos(2*pi*c) for c >= 0 cycles.
inline float cosCycles(float c) {
  c += 0.25f;
  c -= (float)(int32_t)c;  // fractional part; truncation is floor for c >= 0
  const float pos = c * (float)LUT_SIZE;
  uint32_t i = (uint32_t)pos;
  float f = pos - (float)i;
  if (i >= LUT_SIZE) {
    i = 0;
    f = 0.0f;
  }
  return sinLut[i] + f * (sinLut[i + 1] - sinLut[i]);
}

// Symmetric windows, as scipy.signal.windows builds them; x = i / (n - 1).
inline float windowAt(Window w, float x, float alpha) {
  switch (w) {
    case Window::RECT:
      return 1.0f;
    case Window::HANN:
      return 0.5f - 0.5f * cosCycles(x);
    case Window::HAMMING:
      return 0.54f - 0.46f * cosCycles(x);
    case Window::BLACKMAN:
      return 0.42f - 0.5f * cosCycles(x) + 0.08f * cosCycles(2.0f * x);
    case Window::TUKEY:
      if (alpha <= 0.0f) return 1.0f;
      if (alpha >= 1.0f) return 0.5f - 0.5f * cosCycles(x);
      if (x < 0.5f * alpha) return 0.5f - 0.5f * cosCycles(x / alpha);
      if (x > 1.0f - 0.5f * alpha) return 0.5f - 0.5f * cosCycles((1.0f - x) / alpha);
      return 1.0f;
  }
  return 1.0f;
}

// Chip number of sample n, computed the way waveforms.barker_code does: int((n / fs) / chip).
inline int chipIndex(uint32_t n, double fs, double chip) {
  const int k = (int)(((double)n / fs) / chip);
  return k < 0 ? 0 : (k > 12 ? 12 : k);
}

// Fills chipSeq[0..n) and the raised-cosine kernel; returns the kernel length (0 = unshaped).
int prepareBarker(const PulseSpec &s, double fs, uint32_t n) {
  uint32_t start = 0;
  for (int j = 0; j < 13; ++j) {
    uint32_t end = n;
    if (j < 12) {
      // the boundary is within a sample of (j + 1) * chip * fs; step onto it exactly
      const double est = floor((j + 1) * s.chip * fs) - 2.0;
      end = est > start ? (uint32_t)est : start;
      while (end < n && chipIndex(end, fs, s.chip) <= j) ++end;
    }
    for (uint32_t i = start; i < end; ++i) chipSeq[i] = BARKER13[j];
    start = end;
  }
  if (s.rc_shaping <= 0.0) return 0;
  const int k = (int)nearbyint(s.rc_shaping * s.chip * fs);
  if (k < 3) return 0;
  double sum = 0.0;
  for (int m = 0; m < k; ++m) sum += 0.5 - 0.5 * cos(2.0 * M_PI * m / (k - 1));
  for (int m = 0; m < k; ++m) rcKernel[m] = (float)((0.5 - 0.5 * cos(2.0 * M_PI * m / (k - 1))) / sum);
  return k;
}

// Shaped chip value at sample i: the chip sequence (held at its end values) convolved
// with the kernel, aligned as numpy.convolve(..., mode="same") aligns it.
inline float shapedChip(int32_t i, int k, uint32_t n) {
  float acc = 0.0f;
  const int32_t base = i + (k - 1) / 2;
  for (int m = 0; m < k; ++m) {
    int32_t j = base - m;
    j = j < 0 ? 0 : (j >= (int32_t)n ? (int32_t)n - 1 : j);
    acc += rcKernel[m] * (float)chipSeq[j];
  }
  return acc;
}

}  // namespace

const char *kindName(PulseKind k) {
  switch (k) {
    case PulseKind::LFM: return "lfm";
    case PulseKind::GEOMETRIC: return "geometric";
    case PulseKind::BARKER13: return "barker13";
    case PulseKind::CW: return "cw";
  }
  return "?";
}

const char *windowName(Window w) {
  switch (w) {
    case Window::RECT: return "rect";
    case Window::TUKEY: return "tukey";
    case Window::HANN: return "hann";
    case Window::HAMMING: return "hamming";
    case Window::BLACKMAN: return "blackman";
  }
  return "?";
}

bool parseKind(const char *s, PulseKind &out) {
  for (int i = 0; i <= (int)PulseKind::CW; ++i) {
    const PulseKind k = (PulseKind)i;
    if (strcasecmp(s, kindName(k)) == 0) {
      out = k;
      return true;
    }
  }
  return false;
}

bool parseWindow(const char *s, Window &out) {
  for (int i = 0; i <= (int)Window::BLACKMAN; ++i) {
    const Window w = (Window)i;
    if (strcasecmp(s, windowName(w)) == 0) {
      out = w;
      return true;
    }
  }
  return false;
}

void pulseInit() {
  for (uint32_t i = 0; i <= LUT_SIZE; ++i) sinLut[i] = (float)sin(2.0 * M_PI * i / LUT_SIZE);
}

uint32_t pulseSamples(const PulseSpec &s, uint32_t fs) {
  const double duration = s.kind == PulseKind::BARKER13 ? 13 * s.chip : s.duration;
  const double n = nearbyint(duration * fs);
  return n > 0 && n < 4e9 ? (uint32_t)n : 0;
}

const char *pulseValidate(const PulseSpec &s, uint32_t fs, uint32_t maxSamples) {
  const double nyquist = fs / 2.0;
  if (!(s.amplitude > 0.0 && s.amplitude <= 1.0)) return "amplitude must be in (0, 1]";
  if (!(s.tukey_alpha >= 0.0 && s.tukey_alpha <= 1.0)) return "tukey_alpha must be in [0, 1]";
  if (!(s.rc_shaping >= 0.0 && s.rc_shaping <= 1.0)) return "rc_shaping must be in [0, 1]";
  if (s.kind == PulseKind::BARKER13 ? !(s.chip > 0.0) : !(s.duration > 0.0))
    return "duration (chip for barker13) must be positive";
  if (!(s.f0 > 0.0 && s.f0 < nyquist)) return "f0 must be between 0 and fs/2";
  if (s.kind == PulseKind::LFM || s.kind == PulseKind::GEOMETRIC) {
    if (!(s.f1 > 0.0 && s.f1 < nyquist)) return "f1 must be between 0 and fs/2";
    if (s.kind == PulseKind::GEOMETRIC && s.f0 == s.f1) return "geometric sweep needs f0 != f1";
  }
  const uint32_t n = pulseSamples(s, fs);
  if (n < 2) return "pulse shorter than two DAC samples";
  if (n > maxSamples) return "pulse longer than MAX_PULSE_SAMPLES";
  return nullptr;
}

uint32_t pulseSynthesize(const PulseSpec &s, uint32_t fs, uint8_t *out) {
  const uint32_t n = pulseSamples(s, fs);
  const double fsd = (double)fs;
  const float amp = (float)s.amplitude;
  const float alpha = (float)s.tukey_alpha;
  const float invLast = 1.0f / (float)(n - 1);

  uint64_t acc = 0;       // phase, cycles in Q16.48
  int64_t inc = 0;        // phase step for the next sample
  int64_t incStep = 0;    // LFM: change of the phase step per sample
  double geoInc = 0.0, geoRatio = 1.0;
  int rcLen = 0;

  switch (s.kind) {
    case PulseKind::LFM: {
      // phase(n) = a*n + b*n^2 cycles, so step(n) = a + b*(2n + 1)
      const double a = s.f0 / fsd;
      const double b = (s.f1 - s.f0) / s.duration / (2.0 * fsd * fsd);
      inc = llround((a + b) * ONE_CYCLE);
      incStep = llround(2.0 * b * ONE_CYCLE);
      break;
    }
    case PulseKind::GEOMETRIC: {
      // phase(n) = f0*T/L * (exp(L*n/(fs*T)) - 1) cycles: the step grows by a constant ratio
      const double L = log(s.f1 / s.f0);
      const double x = L / (fsd * s.duration);
      geoInc = s.f0 * s.duration / L * expm1(x);
      geoRatio = exp(x);
      break;
    }
    case PulseKind::BARKER13:
      rcLen = prepareBarker(s, fsd, n);
      inc = llround(s.f0 / fsd * ONE_CYCLE);
      break;
    case PulseKind::CW:
      inc = llround(s.f0 / fsd * ONE_CYCLE);
      break;
  }

  for (uint32_t i = 0; i < n; ++i) {
    float v = sinPhase(acc);
    if (s.kind == PulseKind::BARKER13) v *= rcLen ? shapedChip((int32_t)i, rcLen, n) : (float)chipSeq[i];
    const float env = amp * windowAt(s.window, (float)i * invLast, alpha);
    // |v * env| <= 1, so the code is in [1, 255] and truncating code + 0.5 rounds it
    const int32_t code = (int32_t)((float)DAC_MID + 0.5f + 127.0f * (v * env));
    out[i] = (uint8_t)(code < 0 ? 0 : (code > 255 ? 255 : code));

    if (s.kind == PulseKind::GEOMETRIC) {
      acc += (uint64_t)llround(geoInc * ONE_CYCLE);
      geoInc *= geoRatio;
    } else {
      acc += (uint64_t)inc;
      inc += incStep;
    }
  }
  return n;
}
