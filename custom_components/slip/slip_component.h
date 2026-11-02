#pragma once

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/uart/uart.h"
#include "lwip/netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include <string>

namespace esphome {
namespace slip {

class SlipComponent : public Component, public uart::UARTDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  float get_setup_priority() const override { return setup_priority::WIFI; }

  void set_local_ip(const std::string &ip) { local_ip_str_ = ip; }
  void set_gateway(const std::string &ip) { gateway_str_ = ip; }
  void set_subnet(const std::string &ip) { subnet_str_ = ip; }
  void set_diagnostics(bool d) { diagnostics_ = d; }

  // Bridges the ESPHome UART (read in loop()) to the lwIP SLIP RX thread.
  QueueHandle_t rx_queue_{nullptr};

  // Diagnostic counters (written from several tasks, only used for status text)
  volatile uint32_t sio_opened_{0};     // times lwIP called sio_open
  volatile uint32_t sio_consumed_{0};   // bytes lwIP's SLIP thread has read
  volatile uint32_t sio_sent_{0};       // bytes lwIP has written to the UART
  uint32_t uart_rx_{0};                 // bytes seen from the UART in loop()
  uint32_t queue_drops_{0};             // bytes dropped because the queue was full

 protected:
  std::string local_ip_str_;
  std::string gateway_str_;
  std::string subnet_str_;

  bool diagnostics_{false};
  uint32_t last_diag_{0};
  struct netif slip_netif_;

  // Own task that drains the UART into rx_queue_ and, when lwIP has no SLIP
  // receive thread of its own, runs slipif_poll() to hand packets to lwIP.
  // It must not depend on loop(): the main loop is blocked while an OTA
  // upload is being received, and the link has to keep flowing for it.
  TaskHandle_t rx_task_handle_{nullptr};
  static void rx_task_(void *arg);

  // Stall diagnostics, logged from loop() once the main loop runs again.
  volatile uint32_t rx_gap_max_{0};   // longest pause between two passes of rx_task_ (ms)
  volatile uint32_t queue_hwm_{0};    // most bytes seen waiting in rx_queue_
  uint32_t last_stats_{0};
  uint32_t last_drops_{0};
};

}  // namespace slip
}  // namespace esphome
