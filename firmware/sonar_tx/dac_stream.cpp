#include "dac_stream.h"

#include <stdio.h>
#include <string.h>

#include <algorithm>
#include <atomic>

#include "config.h"
#include "driver/dac_continuous.h"
#include "esp_attr.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

namespace {

uint8_t pulseA[MAX_PULSE_SAMPLES];
uint8_t pulseB[MAX_PULSE_SAMPLES];

// Shared with the main loop. The back buffer belongs to the main loop while no swap is
// pending; the refill task swaps the pointers and then clears the flag.
uint8_t *activeBuf = pulseA;
uint8_t *backBuf = pulseB;
uint32_t backLen = 0;
std::atomic<uint32_t> activeLen{0};
std::atomic<bool> swapPending{false};
std::atomic<uint32_t> priRequest{0};
std::atomic<uint32_t> priSamples{0};
std::atomic<bool> runRequest{false};
std::atomic<uint32_t> pings{0};
std::atomic<uint32_t> samplesPerBuffer{0};
volatile uint32_t underruns = 0;

// Refill-task state.
dac_continuous_handle_t dac = nullptr;
QueueHandle_t doneQueue = nullptr;
uint8_t stage[DMA_BUF_BYTES];
bool running = false;
uint32_t framePos = 0;
uint32_t frameLen = 1;

void applyPending() {
  if (swapPending.load(std::memory_order_acquire)) {
    std::swap(activeBuf, backBuf);
    activeLen.store(backLen);
    swapPending.store(false, std::memory_order_release);
  }
  const uint32_t pri = priRequest.exchange(0);
  if (pri) priSamples.store(pri);
}

// Produces the next n output samples. Changes are applied only where a ping starts.
void fill(uint8_t *dst, size_t n, bool mayStart) {
  size_t i = 0;
  while (i < n) {
    if (!running) {
      applyPending();
      if (mayStart && runRequest.load()) {
        running = true;
        framePos = 0;
        continue;
      }
      memset(dst + i, DAC_MID, n - i);
      return;
    }
    if (framePos == 0) {
      if (!runRequest.load()) {
        running = false;
        continue;
      }
      applyPending();
      frameLen = std::max<uint32_t>(std::max(priSamples.load(), activeLen.load()), 1);
      pings.fetch_add(1);
    }
    const uint32_t len = activeLen.load();
    size_t take;
    if (framePos < len) {
      take = std::min<size_t>(n - i, len - framePos);
      memcpy(dst + i, activeBuf + framePos, take);
    } else {
      take = std::min<size_t>(n - i, frameLen - framePos);
      memset(dst + i, DAC_MID, take);
    }
    i += take;
    framePos += take;
    if (framePos >= frameLen) framePos = 0;
  }
}

bool IRAM_ATTR onConvertDone(dac_continuous_handle_t, const dac_event_data_t *event, void *) {
  BaseType_t woken = pdFALSE;
  if (xQueueIsQueueFullFromISR(doneQueue)) {
    // every buffer in the ring finished without a refill: one played stale data
    dac_event_data_t dropped;
    xQueueReceiveFromISR(doneQueue, &dropped, &woken);
    underruns = underruns + 1;
  }
  xQueueSendFromISR(doneQueue, event, &woken);
  return woken == pdTRUE;
}

void refillTask(void *) {
  dac_event_data_t ev;
  size_t want = DMA_BUF_BYTES;
  for (;;) {
    xQueueReceive(doneQueue, &ev, portMAX_DELAY);
    // Until the first refill reports how many samples a DMA buffer holds, output idle
    // only, so the ping position never runs ahead of what was actually queued.
    const bool sized = samplesPerBuffer.load() != 0;
    fill(stage, want, sized);
    size_t loaded = 0;
    dac_continuous_write_asynchronously(dac, static_cast<uint8_t *>(ev.buf), ev.buf_size, stage, want, &loaded);
    if (!sized && loaded) {
      samplesPerBuffer.store(loaded);
      want = loaded;
    }
  }
}

const char *failed(const char *what, esp_err_t err) {
  static char msg[96];
  snprintf(msg, sizeof msg, "%s: %s", what, esp_err_to_name(err));
  return msg;
}

}  // namespace

const char *streamBegin(uint32_t fs) {
  priSamples.store((uint32_t)(DEFAULT_PRI_S * fs + 0.5));
  doneQueue = xQueueCreate(DMA_DESC_NUM, sizeof(dac_event_data_t));
  if (!doneQueue) return "cannot allocate the DMA event queue";

  dac_continuous_config_t cfg = {};
  cfg.chan_mask = DAC_CHANNEL_MASK_CH0;  // GPIO25
  cfg.desc_num = DMA_DESC_NUM;
  cfg.buf_size = DMA_BUF_BYTES;
  cfg.freq_hz = fs;
  cfg.offset = 0;
  cfg.clk_src = DAC_DIGI_CLK_SRC_DEFAULT;  // PLL_D2, 160 MHz
  cfg.chan_mode = DAC_CHANNEL_MODE_SIMUL;
  esp_err_t err = dac_continuous_new_channels(&cfg, &dac);
  if (err != ESP_OK) return failed("dac_continuous_new_channels", err);

  dac_event_callbacks_t cbs = {};
  cbs.on_convert_done = onConvertDone;
  err = dac_continuous_register_event_callback(dac, &cbs, nullptr);
  if (err != ESP_OK) return failed("dac_continuous_register_event_callback", err);

  // core 0: the Arduino loop (serial, ADC, synthesis) runs on core 1
  if (xTaskCreatePinnedToCore(refillTask, "dac_refill", 4096, nullptr, 20, nullptr, 0) != pdPASS)
    return "cannot start the DMA refill task";

  err = dac_continuous_enable(dac);
  if (err != ESP_OK) return failed("dac_continuous_enable", err);
  err = dac_continuous_start_async_writing(dac);
  if (err != ESP_OK) return failed("dac_continuous_start_async_writing", err);
  return nullptr;
}

uint8_t *streamBackBuffer() { return backBuf; }

bool streamSwapPending() { return swapPending.load(std::memory_order_acquire); }

void streamCommit(uint32_t samples) {
  backLen = samples;
  swapPending.store(true, std::memory_order_release);
}

bool streamWaitSwap(uint32_t timeoutMs) {
  const TickType_t start = xTaskGetTickCount();
  while (streamSwapPending()) {
    if (xTaskGetTickCount() - start > pdMS_TO_TICKS(timeoutMs)) return false;
    vTaskDelay(1);
  }
  return true;
}

void streamSetPri(uint32_t samples) { priRequest.store(samples); }
void streamRun(bool on) { runRequest.store(on); }
bool streamRunning() { return runRequest.load(); }

const uint8_t *streamActive(uint32_t *samples) {
  *samples = activeLen.load();
  return activeBuf;
}

uint32_t streamPri() {
  const uint32_t pending = priRequest.load();
  return pending ? pending : priSamples.load();
}

uint32_t streamPings() { return pings.load(); }
uint32_t streamUnderruns() { return underruns; }
uint32_t streamSamplesPerBuffer() { return samplesPerBuffer.load(); }
