// Pulse description and synthesis into 8-bit DAC codes.
//
// Mirrors scope/src/sonarscope/waveforms.py: the same PulseSpec fields and defaults,
// the same phase laws, symmetric windows and Barker-13 shaping, and the same code
// mapping, code = 128 + round(127 * amplitude * window * waveform). `DUMP` on the
// serial port plus firmware/tools/check_codes.py compares the two sample by sample.
#pragma once

#include <stddef.h>
#include <stdint.h>

enum class PulseKind : uint8_t { LFM, GEOMETRIC, BARKER13, CW };
enum class Window : uint8_t { RECT, TUKEY, HANN, HAMMING, BLACKMAN };

struct PulseSpec {
  PulseKind kind = PulseKind::LFM;
  double f0 = 400e3;          // Hz; carrier for Barker-13 and CW
  double f1 = 500e3;          // Hz; sweep end
  double duration = 2e-3;     // s; 13 * chip for Barker-13
  Window window = Window::TUKEY;
  double tukey_alpha = 0.2;
  double amplitude = 1.0;     // fraction of DAC full scale, (0, 1]
  double chip = 40e-6;        // s, Barker-13 only
  double rc_shaping = 0.0;    // fraction of a chip used for raised-cosine phase transitions
};

const char *kindName(PulseKind k);
const char *windowName(Window w);
bool parseKind(const char *s, PulseKind &out);
bool parseWindow(const char *s, Window &out);

// Builds the sine table. Call once before synthesising.
void pulseInit();

// Number of DAC samples the pulse occupies at `fs`.
uint32_t pulseSamples(const PulseSpec &spec, uint32_t fs);

// nullptr when the pulse can be generated, otherwise the reason it cannot.
const char *pulseValidate(const PulseSpec &spec, uint32_t fs, uint32_t maxSamples);

// Writes the DAC codes of a validated pulse to `out`; returns the sample count.
uint32_t pulseSynthesize(const PulseSpec &spec, uint32_t fs, uint8_t *out);
