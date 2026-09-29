#include "env_model.h"

#include <math.h>
#include <strings.h>

namespace {

// Keep in step with scope/src/sonarscope/acoustics.py.
constexpr double FC_MAX_HZ = 450e3, FC_MIN_HZ = 120e3, FC_STEP_HZ = 2.5e3;
constexpr double BW_AT_FC_MIN_HZ = 40e3, BW_AT_FC_MAX_HZ = 100e3;
constexpr double ABSORPTION_BUDGET_DB = 20.0;
constexpr double K_SED_DB_PER_KM_NTU_KHZ = 0.008;
constexpr double PH = 8.0;
constexpr double REF_TEMP_C = 20.0, REF_SALINITY_PSU = 35.0, REF_DEPTH_M = 10.0;
constexpr double T_MIN_S = 1e-3, T_MAX_S = 5e-3, AMP_MIN = 0.5, AMP_MAX = 1.0;
constexpr double BLIND_FRACTION = 0.1, PRI_MIN_S = 20e-3, PRI_MARGIN = 1.25;

double energySpanDb = 0.0;
double tlMinDb = 0.0, tlMaxDb = 1.0;

struct FG {  // Francois-Garrison coefficients for one temperature / salinity / depth
  double a1, f1, a2p2, f2, a3p3;
};

FG fgCoefficients(double T, double S, double D) {
  if (S < 0.0) S = 0.0;
  const double c = 1412.0 + 3.21 * T + 1.19 * S + 0.0167 * D;
  const double theta = 273.0 + T;
  FG k;
  k.a1 = 8.86 / c * pow(10.0, 0.78 * PH - 5.0);
  k.f1 = 2.8 * sqrt(S / 35.0) * pow(10.0, 4.0 - 1245.0 / theta);
  const double a2 = 21.44 * S / c * (1.0 + 0.025 * T);
  const double p2 = 1.0 - 1.37e-4 * D + 6.2e-9 * D * D;
  k.f2 = 8.17 * pow(10.0, 8.0 - 1990.0 / theta) / (1.0 + 0.0018 * (S - 35.0));
  const double a3 = T <= 20.0 ? 4.937e-4 - 2.59e-5 * T + 9.11e-7 * T * T - 1.50e-8 * T * T * T
                              : 3.964e-4 - 1.146e-5 * T + 1.45e-7 * T * T - 6.5e-10 * T * T * T;
  const double p3 = 1.0 - 3.83e-5 * D + 4.9e-10 * D * D;
  k.a2p2 = a2 * p2;
  k.a3p3 = a3 * p3;
  return k;
}

double fgEval(const FG &k, double f_khz) {
  const double ff = f_khz * f_khz;
  const double boric = k.f1 > 0.0 ? k.a1 * k.f1 * ff / (k.f1 * k.f1 + ff) : 0.0;
  return boric + k.a2p2 * k.f2 * ff / (k.f2 * k.f2 + ff) + k.a3p3 * ff;
}

double sediment(double f_hz, double ntu) {
  return K_SED_DB_PER_KM_NTU_KHZ * (ntu > 0.0 ? ntu : 0.0) * f_hz / 1e3;
}

double bandwidthFor(double fc) {
  return BW_AT_FC_MIN_HZ + (fc - FC_MIN_HZ) * (BW_AT_FC_MAX_HZ - BW_AT_FC_MIN_HZ) / (FC_MAX_HZ - FC_MIN_HZ);
}

double q(double x, double step) { return floor(x / step + 0.5) * step; }

double windowPower(Window w, double alpha) {
  switch (w) {
    case Window::RECT: return 1.0;
    case Window::TUKEY: return 1.0 - 5.0 * alpha / 8.0;
    case Window::HANN: return 0.375;
    case Window::HAMMING: return 0.3974;
    case Window::BLACKMAN: return 0.3046;
  }
  return 1.0;
}

struct Band {
  double fc, alphaTop;
  bool limited;
};

Band selectBand(const Environment &env, const FG &k) {
  double fc = FC_MAX_HZ;
  for (;;) {
    const double top = fc + bandwidthFor(fc) / 2.0;
    const double alpha = fgEval(k, top / 1e3) + sediment(top, env.turbidity_ntu);
    if (2.0 * alpha * env.range_m / 1e3 <= ABSORPTION_BUDGET_DB) return {fc, alpha, false};
    if (fc - FC_STEP_HZ < FC_MIN_HZ - 1.0) return {fc, alpha, true};
    fc -= FC_STEP_HZ;
  }
}

double tl2way(const Environment &env, const FG &k, double fc) {
  const double alphaC = fgEval(k, fc / 1e3) + sediment(fc, env.turbidity_ntu);
  return 2.0 * (20.0 * log10(env.range_m) + alphaC * env.range_m / 1e3);
}

double cornerTl(double ntu, double range) {
  Environment e;
  e.turbidity_ntu = ntu;
  e.range_m = range;
  e.temp_c = REF_TEMP_C;
  e.salinity_psu = REF_SALINITY_PSU;
  e.depth_m = REF_DEPTH_M;
  const FG k = fgCoefficients(e.temp_c, e.salinity_psu, e.depth_m);
  return tl2way(e, k, selectBand(e, k).fc);
}

}  // namespace

void envModelInit() {
  energySpanDb = 10.0 * log10(T_MAX_S / T_MIN_S) + 20.0 * log10(AMP_MAX / AMP_MIN);
  tlMinDb = cornerTl(0.0, RANGE_MIN_M);
  tlMaxDb = cornerTl(NTU_MAX, RANGE_MAX_M);
}

double soundSpeed(double T, double S, double D) {
  return 1448.96 + 4.591 * T - 5.304e-2 * T * T + 2.374e-4 * T * T * T + 1.340 * (S - 35.0) +
         1.630e-2 * D + 1.675e-7 * D * D - 1.025e-2 * T * (S - 35.0) - 7.139e-13 * T * D * D * D;
}

double totalAttenuation(double f_hz, const Environment &env) {
  return fgEval(fgCoefficients(env.temp_c, env.salinity_psu, env.depth_m), f_hz / 1e3) +
         sediment(f_hz, env.turbidity_ntu);
}

Decision decide(const Environment &env, ModMode mod, WinMode winMode) {
  const FG k = fgCoefficients(env.temp_c, env.salinity_psu, env.depth_m);
  const double c = soundSpeed(env.temp_c, env.salinity_psu, env.depth_m);
  const Band band = selectBand(env, k);
  const double fc = band.fc, bw = bandwidthFor(fc);
  const double tl = tl2way(env, k, fc);
  double demand = (tl - tlMinDb) / (tlMaxDb - tlMinDb);
  demand = demand < 0.0 ? 0.0 : (demand > 1.0 ? 1.0 : demand);
  const double energyDb = demand * energySpanDb;

  const double tBlind = BLIND_FRACTION * 2.0 * env.range_m / c;
  PulseKind kind;
  switch (mod) {
    case ModMode::LFM: kind = PulseKind::LFM; break;
    case ModMode::GEOMETRIC: kind = PulseKind::GEOMETRIC; break;
    case ModMode::BARKER13: kind = PulseKind::BARKER13; break;
    case ModMode::CW: kind = PulseKind::CW; break;
    default: kind = tBlind < T_MIN_S ? PulseKind::BARKER13 : PulseKind::LFM; break;
  }

  // energy: pulse length first (within the blind-zone limit), then amplitude
  const double tEnergy = T_MIN_S * pow(10.0, energyDb / 10.0);
  const double tCap = fmin(T_MAX_S, fmax(tBlind, T_MIN_S));
  double chip = 0.0, duration;
  if (kind == PulseKind::BARKER13) {
    chip = q(fmax(1.0 / bw, fmin(tBlind, tEnergy) / 13.0), 0.5e-6);
    duration = 13 * chip;
  } else {
    duration = q(fmin(tEnergy, tCap), 0.5e-6);
  }
  const double leftoverDb = energyDb - 10.0 * log10(fmax(duration, 1e-9) / T_MIN_S);
  const double amplitude =
      q(fmin(AMP_MAX, fmax(AMP_MIN, AMP_MIN * pow(10.0, fmax(leftoverDb, 0.0) / 20.0))), 1e-3);

  Window win;
  double alphaW = 0.2;
  if (kind == PulseKind::BARKER13) {
    win = Window::TUKEY;
    alphaW = 0.12;
  } else if (winMode == WinMode::AUTO) {
    win = demand < 0.5 ? Window::HANN : Window::TUKEY;
  } else {
    win = (Window)((int)winMode - 1);
  }

  Decision d;
  PulseSpec &s = d.spec;
  s.kind = kind;
  s.duration = duration;
  s.window = win;
  s.tukey_alpha = alphaW;
  s.amplitude = amplitude;
  s.chip = 40e-6;
  s.rc_shaping = 0.0;
  double bEff;
  if (kind == PulseKind::LFM || kind == PulseKind::GEOMETRIC) {
    s.f0 = fc - bw / 2.0;
    s.f1 = fc + bw / 2.0;
    bEff = bw;
  } else if (kind == PulseKind::BARKER13) {
    s.f0 = s.f1 = fc;
    s.chip = chip;
    s.rc_shaping = 0.2;
    bEff = 1.0 / chip;
  } else {
    s.f0 = s.f1 = fc;
    bEff = 1.0 / duration;
  }

  const double pri = q(fmax(PRI_MIN_S, PRI_MARGIN * 2.0 * env.range_m / c), 1e-4);
  const double energy = amplitude * amplitude * duration * windowPower(win, alphaW);
  const double lightest = AMP_MIN * AMP_MIN * T_MIN_S * windowPower(Window::HANN, 0.2);
  d.pri_s = pri;
  d.sound_speed = c;
  d.fc_hz = fc;
  d.bandwidth_hz = bw;
  d.alpha_db_km = band.alphaTop;
  d.absorption_2way_db = 2.0 * band.alphaTop * env.range_m / 1e3;
  d.tl_2way_db = tl;
  d.demand = demand;
  d.range_limited = band.limited;
  d.achievable_range_m = ABSORPTION_BUDGET_DB / (2.0 * band.alphaTop) * 1e3;
  d.range_resolution_m = c / (2.0 * bEff);
  d.blind_zone_m = c * duration / 2.0;
  d.time_bandwidth = bEff * duration;
  d.energy_db = 10.0 * log10(energy / lightest);
  d.avg_power_db = 10.0 * log10((energy / pri) / (lightest / PRI_MIN_S));
  return d;
}

const char *modName(ModMode m) {
  switch (m) {
    case ModMode::AUTO: return "auto";
    case ModMode::LFM: return "lfm";
    case ModMode::GEOMETRIC: return "geometric";
    case ModMode::BARKER13: return "barker13";
    case ModMode::CW: return "cw";
  }
  return "?";
}

const char *winModeName(WinMode w) {
  return w == WinMode::AUTO ? "auto" : windowName((Window)((int)w - 1));
}

bool parseModMode(const char *s, ModMode &out) {
  for (int i = 0; i <= (int)ModMode::CW; ++i) {
    if (!strcasecmp(s, modName((ModMode)i))) {
      out = (ModMode)i;
      return true;
    }
  }
  return false;
}

bool parseWinMode(const char *s, WinMode &out) {
  for (int i = 0; i <= (int)WinMode::BLACKMAN; ++i) {
    if (!strcasecmp(s, winModeName((WinMode)i))) {
      out = (WinMode)i;
      return true;
    }
  }
  return false;
}
