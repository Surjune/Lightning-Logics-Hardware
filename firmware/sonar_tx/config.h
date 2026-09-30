// Transmitter configuration. Values that the analyzer also assumes are marked
// with the sonarscope file they must stay in step with.
#pragma once

#include <stddef.h>
#include <stdint.h>

// ---- DAC stream ---------------------------------------------------------------
// I2S0 -> built-in 8-bit DAC via DMA. PLL_D2 (160 MHz) / 80 = 2 MHz.
constexpr uint32_t FS_DAC = 2000000;          // scope/src/sonarscope/config.py FS_DAC
constexpr uint8_t DAC_MID = 128;              // idle level (config.py DAC_MID)
constexpr uint32_t DMA_DESC_NUM = 4;          // DMA ring: 4 buffers of ~1 ms each
constexpr size_t DMA_BUF_BYTES = 4000;        // per descriptor, must be <= 4092
constexpr uint32_t MAX_PULSE_SAMPLES = 20000; // 10 ms at 2 MSPS (two such buffers are kept)

// ---- pins (38-pin ESP32 DevKit) --------------------------------------------------
// GPIO25 is DAC channel 0 and is claimed by the DAC driver: that is the transmit output.
constexpr int PIN_BUTTON = 0;          // on-board BOOT button (active low)
constexpr int PIN_MARKER = 27;         // T0 marker -> scope CH2 (steps high on every change)

// Environment inputs: a pot wiper on an ADC1 pin, pot ends on 3V3 and GND. An input whose
// *_WIRED flag is 0 uses its default, or a value set with `ENV` (the dashboard's sliders).
// Leave the flag at 0 for any pin with nothing connected: a floating pin reads noise.
constexpr int PIN_POT_TURBIDITY = 34;  // 0-100 NTU
constexpr int PIN_POT_RANGE = 35;      // 5-200 m, logarithmic
constexpr int PIN_POT_TEMP = 32;       // 0-35 degC
constexpr int PIN_POT_SALINITY = 33;   // 0-40 PSU
constexpr int PIN_POT_DEPTH = 36;      // 0-300 m (board label SP / VP)
#define TURBIDITY_POT_WIRED 1
#define RANGE_POT_WIRED 0
#define TEMP_POT_WIRED 0
#define SALINITY_POT_WIRED 0
#define DEPTH_POT_WIRED 0

// Master switch: 0 ignores every pot (all inputs use defaults or `ENV` values).
#define USE_POTS 1

// ---- timing -----------------------------------------------------------------------
constexpr uint32_t ADC_PERIOD_MS = 10;     // plan.py TestCase.adc_period
constexpr double DEFAULT_PRI_S = 20e-3;    // plan.py TestCase.pri
constexpr uint32_t MARKER_HOLD_MS = 200;   // longer than the transition capture after T0
constexpr uint32_t LONG_PRESS_MS = 600;
constexpr uint32_t SERIAL_BAUD = 115200;   // dut.py SerialDut default
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;  // `TELEM ON` status lines for the dashboard

// 240 MHz synthesises a new pulse fastest. Lower values (160, 80) cut current draw;
// the DAC clock comes from PLL_D2 and does not change, but re-check a CW tone on the
// scope after changing this.
constexpr uint32_t CPU_FREQ_MHZ = 240;
