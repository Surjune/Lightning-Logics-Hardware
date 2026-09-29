// Continuous DMA stream to the built-in DAC (GPIO25) with double-buffered pulses.
//
// The DMA ring plays a repeating frame: the active pulse followed by mid-scale idle
// until the ping repetition interval (PRI) is over. The PRI is counted in DAC samples,
// so ping timing is as exact as the DAC clock. A new pulse is written into the back
// buffer and swapped in at the next ping boundary, so no ping mixes the old and new
// waveforms. A small task refills each DMA buffer as it finishes (memcpy/memset only);
// the CPU never writes individual samples to the DAC.
#pragma once

#include <stddef.h>
#include <stdint.h>

// Starts the DAC and the refill task, idle at mid-scale. Returns an error message or nullptr.
const char *streamBegin(uint32_t fs);

// Back buffer (MAX_PULSE_SAMPLES bytes). Only write it while no swap is pending.
uint8_t *streamBackBuffer();
bool streamSwapPending();
// Swap the back buffer in at the next ping boundary (immediately while idle).
void streamCommit(uint32_t samples);
// Blocks until a pending swap has happened; false on timeout.
bool streamWaitSwap(uint32_t timeoutMs);

void streamSetPri(uint32_t samples);   // takes effect at the next ping boundary
void streamRun(bool on);               // stopping lets the current ping finish
bool streamRunning();

const uint8_t *streamActive(uint32_t *samples);  // the pulse on air (stable while no swap is pending)
uint32_t streamPri();
uint32_t streamPings();
uint32_t streamUnderruns();            // DMA buffers that were not refilled in time
uint32_t streamSamplesPerBuffer();
