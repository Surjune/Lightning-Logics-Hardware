"""Transmitter constants shared by the reference model and the analyzer.

These mirror the firmware configuration: ESP32 I2S0 driving the built-in DAC.
"""

# DAC update rate. PLL_F160M / (32 * 2.5) = 2.0 MHz. The I2S clock tree tops out at
# 2.5 MHz, and the ESP-IDF DAC driver documentation advises staying at or below 2 MHz.
FS_DAC = 2.0e6

# Built-in DAC resolution and code range (mid-scale is the idle level).
DAC_BITS = 8
DAC_MID = 128
DAC_MAX_CODE = 255
DAC_VREF = 3.3  # V, the DAC is ratiometric to VDD3P3_RTC

# Nominal speed of sound used for range-resolution figures.
C_WATER = 1500.0  # m/s

# Required transmit band.
BAND_MIN_HZ = 100e3
BAND_MAX_HZ = 500e3
