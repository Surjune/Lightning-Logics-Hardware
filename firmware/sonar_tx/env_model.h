// Environment -> pulse decision.
//
// Mirrors scope/src/sonarscope/acoustics.py: the same equations, constants and rounding,
// so firmware/tools/check_adaptation.py can compare the board's decisions with it.
// Physics: Mackenzie (1981) sound speed, Francois-Garrison (1982) seawater absorption and
// a linear-in-frequency suspended-sediment term; see acoustics.py for the rule itself.
#pragma once

#include "pulse.h"

struct Environment {
  double turbidity_ntu = 0.0;
  double range_m = 60.0;
  double temp_c = 25.0;
  double salinity_psu = 35.0;
  double depth_m = 10.0;
};

enum class ModMode : uint8_t { AUTO, LFM, GEOMETRIC, BARKER13, CW };
enum class WinMode : uint8_t { AUTO, RECT, TUKEY, HANN, HAMMING, BLACKMAN };

struct Decision {
  PulseSpec spec;
  double pri_s;
  double sound_speed;
  double fc_hz;
  double bandwidth_hz;
  double alpha_db_km;         // total attenuation at the band's top edge
  double absorption_2way_db;  // over the required range, at the top edge
  double tl_2way_db;          // spreading + absorption at the centre frequency
  double demand;              // 0..1 share of the pulse-energy range used
  bool range_limited;         // even the lowest band exceeds the absorption budget
  double achievable_range_m;  // range at which the chosen band uses the whole budget
  double range_resolution_m;
  double blind_zone_m;
  double time_bandwidth;
  double energy_db;           // relative to the lightest ping (1 ms, amplitude 0.5, Hann)
  double avg_power_db;        // energy / PRI relative to the lightest ping at the minimum PRI
};

// Operating envelope of the inputs.
constexpr double RANGE_MIN_M = 5.0, RANGE_MAX_M = 200.0, NTU_MAX = 100.0;

void envModelInit();  // computes the transmission-loss envelope; call once
Decision decide(const Environment &env, ModMode mod, WinMode win);
double soundSpeed(double temp_c, double salinity_psu, double depth_m);
double totalAttenuation(double f_hz, const Environment &env);  // dB/km, seawater + sediment

const char *modName(ModMode m);
const char *winModeName(WinMode w);
bool parseModMode(const char *s, ModMode &out);
bool parseWinMode(const char *s, WinMode &out);
