#include "slip_component.h"
#include "esphome/core/log.h"
#include "esphome/components/network/util.h"
#include "lwip/ip4_addr.h"
#include "lwip/tcpip.h"
#include "lwip/sio.h"
#include "netif/slipif.h"

namespace esphome {
namespace slip {

static const char *const TAG = "slip";

// lwIP's sio_* callbacks are plain C functions; they find us through this.
SlipComponent *global_slip_device = nullptr;

void SlipComponent::setup() {
  // NOTE: the logger must be off (baud_rate: 0) when SLIP uses UART0.
  global_slip_device = this;

  this->rx_queue_ = xQueueCreate(1024, sizeof(uint8_t));
  if (this->rx_queue_ == nullptr) {
    this->mark_failed();
    return;
  }

  ip4_addr_t ipaddr, netmask, gw;
  if (!ip4addr_aton(this->local_ip_str_.c_str(), &ipaddr) ||
      !ip4addr_aton(this->gateway_str_.c_str(), &gw) ||
      !ip4addr_aton(this->subnet_str_.c_str(), &netmask)) {
    this->mark_failed();
    return;
  }

  // lwIP runs in its own tcpip thread: take the core lock while touching
  // netifs, and hand received packets over with tcpip_input (not ip_input).
  LOCK_TCPIP_CORE();
  netif_add(&this->slip_netif_, &ipaddr, &netmask, &gw,
            nullptr,  // state = serial port number for slipif; unused here
            slipif_init, tcpip_input);
  netif_set_default(&this->slip_netif_);
  netif_set_up(&this->slip_netif_);
  netif_set_link_up(&this->slip_netif_);
  UNLOCK_TCPIP_CORE();

  // Drain the UART in a dedicated task instead of loop() (see header).
  xTaskCreate(&SlipComponent::rx_task_, "slip_rx", 3072, this, 5, &this->rx_task_handle_);
}

// Moves bytes from ESPHome's UART buffer into the queue read by lwIP's SLIP
// thread. Sleeps one tick between passes so the main loop still gets CPU time.
void SlipComponent::rx_task_(void *arg) {
  auto *self = static_cast<SlipComponent *>(arg);
  uint32_t last = millis();
  for (;;) {
    uint32_t now = millis();
    if (now - last > self->rx_gap_max_)
      self->rx_gap_max_ = now - last;
    last = now;
    while (self->available()) {
      uint8_t byte;
      if (!self->read_byte(&byte))
        break;
      self->uart_rx_++;
      if (xQueueSend(self->rx_queue_, &byte, 0) != pdTRUE)  // never block
        self->queue_drops_++;
    }
#if !SLIP_USE_RX_THREAD
    // lwIP has no SLIP receive thread in this build: hand the queued bytes to
    // lwIP from here (not from loop(), which is blocked during OTA).
    slipif_poll(&self->slip_netif_);
#endif
    UBaseType_t waiting = uxQueueMessagesWaiting(self->rx_queue_);
    if (waiting > self->queue_hwm_)
      self->queue_hwm_ = waiting;
    vTaskDelay(1);
  }
}

void SlipComponent::loop() {
  // Tell ESPHome (api, etc.) that this node has a working network interface.
  network::custom_interface_connected() =
      netif_is_up(&this->slip_netif_) && netif_is_link_up(&this->slip_netif_);

  // (UART -> lwIP queue transfer now happens in rx_task_.)

  // After a stall (e.g. during OTA) the main loop resumes here; report what
  // the receive path did meanwhile. Quiet when everything looks normal.
  if (millis() - this->last_stats_ >= 5000) {
    this->last_stats_ = millis();
    if (this->rx_gap_max_ > 500 || this->queue_hwm_ > 256 || this->queue_drops_ != this->last_drops_) {
      ESP_LOGW(TAG, "rx_task max gap %u ms, queue high-water %u, queue drops %u (+%u), uart_rx %u, consumed %u",
               (unsigned) this->rx_gap_max_, (unsigned) this->queue_hwm_, (unsigned) this->queue_drops_,
               (unsigned) (this->queue_drops_ - this->last_drops_), (unsigned) this->uart_rx_,
               (unsigned) this->sio_consumed_);
      this->last_drops_ = this->queue_drops_;
    }
    this->rx_gap_max_ = 0;
    this->queue_hwm_ = 0;
  }
  if (this->diagnostics_ && millis() - this->last_diag_ >= 2000) {
    this->last_diag_ = millis();
    char buf[200];
    snprintf(buf, sizeof(buf),
             "SLIPDIAG up=%d link=%d default=%d ip=%s uart_rx=%u consumed=%u sent=%u opened=%u drops=%u",
             netif_is_up(&this->slip_netif_), netif_is_link_up(&this->slip_netif_),
             netif_default == &this->slip_netif_, ip4addr_ntoa(netif_ip4_addr(&this->slip_netif_)),
             (unsigned) this->uart_rx_, (unsigned) this->sio_consumed_, (unsigned) this->sio_sent_,
             (unsigned) this->sio_opened_, (unsigned) this->queue_drops_);
    // Wrapped in SLIP END bytes so a host reading the stream can split it off.
    this->write_byte(0xC0);
    this->write_str(buf);
    this->write_byte(0xC0);
  }
}

void SlipComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "SLIP:");
  ESP_LOGCONFIG(TAG, "  Local IP: %s", this->local_ip_str_.c_str());
  ESP_LOGCONFIG(TAG, "  Gateway: %s", this->gateway_str_.c_str());
  ESP_LOGCONFIG(TAG, "  Subnet: %s", this->subnet_str_.c_str());
#if SLIP_USE_RX_THREAD
  ESP_LOGCONFIG(TAG, "  lwIP SLIP RX thread: yes (SLIP_USE_RX_THREAD=1)");
#else
  ESP_LOGCONFIG(TAG, "  lwIP SLIP RX thread: no, polled from rx_task (SLIP_USE_RX_THREAD=0)");
#endif
}

}  // namespace slip
}  // namespace esphome

// ---------------------------------------------------------
// lwIP serial I/O (sio) hooks required by slipif
// ---------------------------------------------------------
extern "C" {

sio_fd_t sio_open(u8_t devnum) {
  if (esphome::slip::global_slip_device != nullptr)
    esphome::slip::global_slip_device->sio_opened_++;
  return (sio_fd_t) esphome::slip::global_slip_device;
}

void sio_send(u8_t c, sio_fd_t fd) {
  auto *slip = (esphome::slip::SlipComponent *) fd;
  if (slip != nullptr) {
    slip->write_byte(c);
    slip->sio_sent_++;
  }
}

// Blocks until at least one byte is available (called from lwIP's SLIP thread).
u32_t sio_read(sio_fd_t fd, u8_t *data, u32_t len) {
  auto *slip = (esphome::slip::SlipComponent *) fd;
  if (slip == nullptr || len == 0)
    return 0;
  if (xQueueReceive(slip->rx_queue_, data, portMAX_DELAY) != pdTRUE)
    return 0;
  u32_t n = 1;
  while (n < len && xQueueReceive(slip->rx_queue_, data + n, 0) == pdTRUE)
    n++;
  slip->sio_consumed_ += n;
  return n;
}

u32_t sio_tryread(sio_fd_t fd, u8_t *data, u32_t len) {
  auto *slip = (esphome::slip::SlipComponent *) fd;
  if (slip == nullptr || len == 0)
    return 0;
  u32_t n = 0;
  while (n < len && xQueueReceive(slip->rx_queue_, data + n, 0) == pdTRUE)
    n++;
  slip->sio_consumed_ += n;
  return n;
}

void sio_read_abort(sio_fd_t fd) {}

}  // extern "C"
