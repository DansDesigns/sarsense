/*
 * SARSense sensor node for the Arduino IDE.
 *
 * Needs the "esp32" boards package by Espressif, version 3.0 or newer
 * (3.3 or newer for the ESP32-C5). Compile-tested with 3.3.11 on ESP32,
 * ESP32-S3, ESP32-C6 and ESP32-C5; ESP32-S2 and C3 should also work.
 *
 * What it does: joins your Wi-Fi, pings the router about 20 times a second,
 * listens to every data frame on the channel and sends the channel state
 * information (CSI) from the router and from other SARSense nodes to the
 * SARSense hub over UDP. Same behaviour and packet format as the ESP-IDF
 * version in firmware/esp32_csi_node.
 *
 * Settings: edit the block below before uploading, or change them later over
 * the Serial Monitor (115200 baud, line ending "Newline"). Type "help".
 * Settings typed over serial are kept across restarts and win over the
 * values below.
 */

// ------------------------------------------------------------------ settings
#define SARS_WIFI_SSID      "sarsense"
#define SARS_WIFI_PASSWORD  ""
#define SARS_HUB_IP         ""       // blank = find the hub automatically
#define SARS_HUB_PORT       5566
#define SARS_TX_HZ          20       // pings per second to the router
#define SARS_MAX_HZ         25       // most CSI reports per second per transmitter
#define SARS_PING_ROUTER    true
// ---------------------------------------------------------------------------

#include <WiFi.h>
#include <WiFiUdp.h>
#include <Preferences.h>
#include "esp_wifi.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"

#if !defined(CONFIG_ESP_WIFI_CSI_ENABLED) || !CONFIG_ESP_WIFI_CSI_ENABLED
#error "This ESP32 core was built without CSI support. Use the Espressif esp32 boards package 3.0 or newer."
#endif

#define FW_VERSION "0.1.0-ar"
#define CSI_BYTES  128   // first 64 subcarriers (L-LTF)
#define MAX_PEERS  32
#define QUEUE_LEN  64
#define T_CSI      1
#define T_HELLO    2
#define T_PEERS    5
#define HUB_TIMEOUT_US (30LL * 1000000)

struct CsiItem {
  uint8_t  src[6];
  int8_t   rssi;
  int8_t   noise;
  uint8_t  channel;
  uint8_t  flags;
  uint32_t ts;
  uint16_t len;
  int8_t   buf[CSI_BYTES];
};

struct Source {
  uint8_t  mac[6];
  uint32_t lastTs;
};

static QueueHandle_t queue;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static Source sources[MAX_PEERS + 1];   // slot 0 is the router
static volatile int nSources = 0;
static uint32_t minGapUs;
static volatile uint32_t dropped = 0;

static uint8_t selfMac[6];
static WiFiUDP udp;
static IPAddress hubIp;
static uint16_t hubPort = SARS_HUB_PORT;
static bool hubKnown = false;
static bool hubFixed = false;
static int64_t hubHeardUs = 0;
static uint32_t seq = 0;
static esp_ping_handle_t pingHandle = nullptr;
static volatile bool gotIp = false;
static volatile bool needPing = false;

static char hubName[32] = "";     // the name the hub gives this node
static Preferences prefs;
static String cfgSsid, cfgPass, cfgHub;

// ------------------------------------------------------------- CSI intake

// Runs inside the Wi-Fi task: short, never blocks.
static void onCsi(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf || info->len < 16) return;
  bool wanted = false;
  portENTER_CRITICAL(&lock);
  for (int i = 0; i < nSources; i++) {
    Source &s = sources[i];
    if (memcmp(s.mac, info->mac, 6) == 0) {
      uint32_t now = info->rx_ctrl.timestamp;
      if ((uint32_t)(now - s.lastTs) >= minGapUs) {
        s.lastTs = now;
        wanted = true;
      }
      break;
    }
  }
  portEXIT_CRITICAL(&lock);
  if (!wanted) return;

  CsiItem it;
  memcpy(it.src, info->mac, 6);
  it.rssi = info->rx_ctrl.rssi;
  it.noise = (int8_t)info->rx_ctrl.noise_floor;
  it.channel = info->rx_ctrl.channel;
  it.flags = info->first_word_invalid ? 1 : 0;
  it.ts = info->rx_ctrl.timestamp;
  it.len = info->len < CSI_BYTES ? info->len : CSI_BYTES;
  memcpy(it.buf, info->buf, it.len);
  if (xQueueSend(queue, &it, 0) != pdTRUE) dropped = dropped + 1;
}

static void startCsi() {
#if CONFIG_SOC_WIFI_HE_SUPPORT
  wifi_csi_config_t csi = {};
  csi.enable = 1;
  csi.acquire_csi_legacy = 1;
  #if CONFIG_SOC_WIFI_MAC_VERSION_NUM == 3
  csi.acquire_csi_force_lltf = 1;   // ESP32-C5: same 64-tone L-LTF from every frame type
  #else
  csi.acquire_csi_ht20 = 1;         // ESP32-C6: the hub locks each link to one format
  csi.acquire_csi_su = 1;
  #endif
#else
  wifi_csi_config_t csi = {};
  csi.lltf_en = true;
  csi.htltf_en = false;
  csi.stbc_htltf2_en = false;
  csi.ltf_merge_en = true;
  csi.channel_filter_en = false;    // keep subcarriers independent
  csi.manu_scale = false;
  csi.shift = 0;
#endif
  wifi_promiscuous_filter_t filter = {};
  filter.filter_mask = WIFI_PROMIS_FILTER_MASK_DATA;
  esp_wifi_set_promiscuous_filter(&filter);
  esp_wifi_set_promiscuous(true);
  if (esp_wifi_set_csi_config(&csi) != ESP_OK) Serial.println("CSI config rejected");
  esp_wifi_set_csi_rx_cb(onCsi, nullptr);
  if (esp_wifi_set_csi(true) != ESP_OK) Serial.println("CSI could not be enabled");
}

// ---------------------------------------------------------------- packets

static size_t putHeader(uint8_t *p, uint8_t type) {
  memcpy(p, "SRS1", 4);
  p[4] = type;
  p[5] = 1;
  memcpy(p + 6, selfMac, 6);
  return 12;
}

static void sendToHub(const uint8_t *buf, size_t len) {
  IPAddress dest = hubKnown ? hubIp : IPAddress(255, 255, 255, 255);
  udp.beginPacket(dest, hubPort);
  udp.write(buf, len);
  udp.endPacket();
}

static void sendCsi(const CsiItem &it) {
  uint8_t p[12 + 20 + CSI_BYTES];
  size_t n = putHeader(p, T_CSI);
  memcpy(p + n, it.src, 6); n += 6;
  p[n++] = (uint8_t)it.rssi;
  p[n++] = (uint8_t)it.noise;
  p[n++] = it.channel;
  p[n++] = it.flags;
  uint32_t s = seq++;
  memcpy(p + n, &s, 4); n += 4;
  memcpy(p + n, &it.ts, 4); n += 4;
  memcpy(p + n, &it.len, 2); n += 2;
  memcpy(p + n, it.buf, it.len); n += it.len;
  sendToHub(p, n);
}

static void sendHello() {
  uint8_t p[12 + 20];
  size_t n = putHeader(p, T_HELLO);
  uint8_t *bssid = WiFi.BSSID();
  if (!bssid) return;
  memcpy(p + n, bssid, 6); n += 6;
  p[n++] = (uint8_t)WiFi.channel();
  p[n++] = (uint8_t)(int8_t)WiFi.RSSI();
  uint32_t up = (uint32_t)(esp_timer_get_time() / 1000000);
  memcpy(p + n, &up, 4); n += 4;
  char fw[8] = {0};
  strncpy(fw, FW_VERSION, sizeof(fw));
  memcpy(p + n, fw, 8); n += 8;
  sendToHub(p, n);
}

static void handlePeers(const uint8_t *buf, int len, IPAddress from, uint16_t fromPort) {
  if (len < 13 || memcmp(buf, "SRS1", 4) != 0 || buf[4] != T_PEERS) return;
  int count = buf[12];
  if (count > MAX_PEERS || len < 13 + count * 6) return;
  int tail = 13 + count * 6;
  if (len > tail) {                       // the hub also sends this node's name
    int nl = buf[tail];
    if (nl > 0 && nl < (int)sizeof(hubName) && len >= tail + 1 + nl) {
      char nm[32] = {0};
      memcpy(nm, buf + tail + 1, nl);
      if (strcmp(nm, hubName) != 0) {
        strncpy(hubName, nm, sizeof(hubName) - 1);
        Serial.printf("The hub calls this node \"%s\"\n", hubName);
      }
    }
  }
  uint8_t *bssid = WiFi.BSSID();

  portENTER_CRITICAL(&lock);
  int n = 0;
  if (bssid) {
    memcpy(sources[n].mac, bssid, 6);
    n++;
  }
  for (int i = 0; i < count; i++) {
    const uint8_t *m = buf + 13 + i * 6;
    if (memcmp(m, selfMac, 6) == 0 || (bssid && memcmp(m, bssid, 6) == 0)) continue;
    memcpy(sources[n].mac, m, 6);
    sources[n].lastTs = 0;
    n++;
  }
  nSources = n;
  portEXIT_CRITICAL(&lock);

  if (!hubKnown || hubIp != from) {
    Serial.printf("Hub found at %s, listening to %d transmitters\n", from.toString().c_str(), n);
  }
  hubIp = from;
  hubPort = fromPort;
  hubKnown = true;
  hubHeardUs = esp_timer_get_time();
}

// ------------------------------------------------------------------- ping

static void startPing() {
  if (!SARS_PING_ROUTER) return;
  if (pingHandle) {
    esp_ping_stop(pingHandle);
    esp_ping_delete_session(pingHandle);
    pingHandle = nullptr;
  }
  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  ip_2_ip4(&cfg.target_addr)->addr = (uint32_t)WiFi.gatewayIP();
  IP_SET_TYPE_VAL(cfg.target_addr, IPADDR_TYPE_V4);
  cfg.count = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms = 1000 / SARS_TX_HZ;
  cfg.timeout_ms = 1000;
  cfg.data_size = 8;
  esp_ping_callbacks_t cbs = {};
  if (esp_ping_new_session(&cfg, &cbs, &pingHandle) == ESP_OK) {
    esp_ping_start(pingHandle);
    Serial.printf("Pinging router %s %d times a second\n", WiFi.gatewayIP().toString().c_str(), SARS_TX_HZ);
  } else {
    Serial.println("Could not start pinging the router");
  }
}

// ------------------------------------------------------------------- Wi-Fi

static void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      portENTER_CRITICAL(&lock);
      memcpy(sources[0].mac, info.wifi_sta_connected.bssid, 6);
      sources[0].lastTs = 0;
      if (nSources == 0) nSources = 1;
      portEXIT_CRITICAL(&lock);
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      gotIp = true;
      needPing = true;   // start it from loop(), not from the event task
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      gotIp = false;
      portENTER_CRITICAL(&lock);
      nSources = 0;
      portEXIT_CRITICAL(&lock);
      break;
    default:
      break;
  }
}

// ------------------------------------------------------------ serial setup

static void loadSettings() {
  prefs.begin("sarsense", false);
  cfgSsid = prefs.getString("ssid", SARS_WIFI_SSID);
  cfgPass = prefs.getString("pass", SARS_WIFI_PASSWORD);
  cfgHub = prefs.getString("hub", SARS_HUB_IP);
  hubFixed = cfgHub.length() > 0 && hubIp.fromString(cfgHub);
  hubKnown = hubFixed;
  if (hubFixed) hubHeardUs = esp_timer_get_time();
}

static void printStatus() {
  Serial.printf("Node %s (%02x:%02x:%02x:%02x:%02x:%02x) firmware %s\n",
                hubName[0] ? hubName : "unnamed until the hub answers",
                selfMac[0], selfMac[1], selfMac[2], selfMac[3], selfMac[4], selfMac[5], FW_VERSION);
  Serial.printf("Wi-Fi \"%s\" %s", cfgSsid.c_str(), gotIp ? "connected" : "not connected");
  if (gotIp) Serial.printf(", address %s, channel %d, %d dBm", WiFi.localIP().toString().c_str(), WiFi.channel(), WiFi.RSSI());
  Serial.println();
  Serial.printf("Hub %s\n", hubKnown ? hubIp.toString().c_str() : "not found yet (searching)");
  Serial.printf("Reports sent %u, dropped %u, transmitters %d, free memory %u\n",
                seq, dropped, nSources, ESP.getFreeHeap());
}

static void handleSerial() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (line.length() < 120) line += c;
      continue;
    }
    line.trim();
    int sp = line.indexOf(' ');
    String cmd = sp < 0 ? line : line.substring(0, sp);
    String arg = sp < 0 ? String() : line.substring(sp + 1);
    line = "";
    cmd.toLowerCase();
    if (cmd == "ssid") {
      prefs.putString("ssid", arg);
      Serial.println("Saved. Type restart to use it.");
    } else if (cmd == "pass") {
      prefs.putString("pass", arg);
      Serial.println("Saved. Type restart to use it.");
    } else if (cmd == "hub") {
      prefs.putString("hub", arg);
      Serial.println(arg.length() ? "Saved. Type restart to use it." : "Hub cleared, the node will search for it after restart.");
    } else if (cmd == "status") {
      printStatus();
    } else if (cmd == "reset") {
      prefs.clear();
      Serial.println("Serial settings cleared, the values in the sketch apply after restart.");
    } else if (cmd == "restart") {
      ESP.restart();
    } else if (cmd.length()) {
      Serial.println("Commands:");
      Serial.println("  ssid <name>      Wi-Fi network name");
      Serial.println("  pass <password>  Wi-Fi password (blank for an open network)");
      Serial.println("  hub <ip>         hub address, or just 'hub' to search automatically");
      Serial.println("  status           show connection and counters");
      Serial.println("  reset            forget serial settings");
      Serial.println("  restart          restart the node");
    }
  }
}

// --------------------------------------------------------------- Arduino

void setup() {
  Serial.begin(115200);
  delay(300);
  queue = xQueueCreate(QUEUE_LEN, sizeof(CsiItem));
  minGapUs = 1000000UL / SARS_MAX_HZ;

  WiFi.mode(WIFI_STA);
  // read the MAC from the chip itself: WiFi.macAddress() can still be all
  // zeros this early, and two nodes reporting 00:00:00:00:00:00 look like one
  // station to the hub
  if (esp_read_mac(selfMac, ESP_MAC_WIFI_STA) != ESP_OK || !memcmp(selfMac, "\0\0\0\0\0\0", 6)) {
    WiFi.macAddress(selfMac);
  }
  if (!memcmp(selfMac, "\0\0\0\0\0\0", 6)) {
    Serial.println("Could not read this board's MAC address. The hub cannot tell nodes apart without it.");
  }
  loadSettings();

  WiFi.onEvent(onWifiEvent);
  WiFi.setSleep(false);          // power save drops frames and ruins timing
  WiFi.setAutoReconnect(true);
  WiFi.begin(cfgSsid.c_str(), cfgPass.length() ? cfgPass.c_str() : nullptr);
  startCsi();
  udp.begin(SARS_HUB_PORT + 1);  // the hub replies to this port

  Serial.println();
  Serial.println("SARSense node starting. Type help for settings.");
  printStatus();
}

void loop() {
  static int64_t lastHello = 0;
  static int64_t lastStats = 0;
  static bool wasConnected = false;

  handleSerial();

  if (gotIp != wasConnected) {
    wasConnected = gotIp;
    Serial.println(gotIp ? "Wi-Fi connected" : "Wi-Fi lost, reconnecting");
    if (gotIp) printStatus();
  }
  if (needPing) {
    needPing = false;
    startPing();
  }

  CsiItem it;
  int budget = 200;              // keep serial and hello handling responsive
  while (budget-- > 0 && xQueueReceive(queue, &it, 0) == pdTRUE) {
    if (gotIp && hubKnown) sendCsi(it);
  }

  if (gotIp) {
    int size = udp.parsePacket();
    if (size > 0) {
      uint8_t rx[64 + MAX_PEERS * 6];   // peers plus this node's name
      int n = udp.read(rx, sizeof(rx));
      handlePeers(rx, n, udp.remoteIP(), udp.remotePort());
    }
  }

  int64_t now = esp_timer_get_time();
  if (hubKnown && !hubFixed && now - hubHeardUs > HUB_TIMEOUT_US) {
    Serial.println("Hub silent, searching again");
    hubKnown = false;
    hubPort = SARS_HUB_PORT;
  }
  if (gotIp && now - lastHello > 5000000) {
    lastHello = now;
    sendHello();
  }
  if (now - lastStats > 60000000) {
    lastStats = now;
    printStatus();
  }
  delay(2);
}
