/*
 * SARSense sensor node (ESP-IDF 5.x).
 *
 * Joins the Wi-Fi network, pings the router to create a steady stream of
 * frames, listens to every data frame in the air, and forwards CSI from the
 * router and from other SARSense nodes to the hub over UDP.
 *
 * Packet layout is documented in sarsense/protocol.py. All fields are
 * little-endian, which matches every ESP32 variant.
 */
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "ping/ping_sock.h"
#include "sdkconfig.h"

#define TAG          "sarsense"
#define FW_VERSION   "0.1.0"
#define CSI_BYTES    128          /* first 64 subcarriers (L-LTF) */
#define MAX_PEERS    32
#define QUEUE_LEN    64
#define T_CSI        1
#define T_HELLO      2
#define T_PEERS      5
#define GOT_IP_BIT   BIT0
#define HUB_TIMEOUT_US (30LL * 1000000)

typedef struct {
    uint8_t  src[6];
    int8_t   rssi;
    int8_t   noise;
    uint8_t  channel;
    uint8_t  flags;
    uint32_t ts;
    uint16_t len;
    int8_t   buf[CSI_BYTES];
} csi_item_t;

typedef struct {
    uint8_t mac[6];
    uint32_t last_ts;   /* rx timestamp of last forwarded frame, microseconds */
} source_t;

static QueueHandle_t s_queue;
static EventGroupHandle_t s_events;
static uint8_t s_self[6];

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static source_t s_sources[MAX_PEERS + 1];   /* slot 0 is the router */
static int s_nsources;                      /* 0 until associated */
static uint32_t s_min_gap_us;

static int s_sock = -1;
static struct sockaddr_in s_hub;
static bool s_hub_known;
static int64_t s_hub_heard_us;
static uint32_t s_seq, s_dropped;
static esp_ping_handle_t s_ping;

/* ------------------------------------------------------------- CSI intake */

/* Runs in the Wi-Fi task: keep it short, never block. */
static void csi_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || info->len < 16) {
        return;
    }
    bool wanted = false;
    portENTER_CRITICAL(&s_lock);
    for (int i = 0; i < s_nsources; i++) {
        source_t *s = &s_sources[i];
        if (memcmp(s->mac, info->mac, 6) == 0) {
            uint32_t now = info->rx_ctrl.timestamp;
            if ((uint32_t)(now - s->last_ts) >= s_min_gap_us) {
                s->last_ts = now;
                wanted = true;
            }
            break;
        }
    }
    portEXIT_CRITICAL(&s_lock);
    if (!wanted) {
        return;
    }

    csi_item_t it;
    memcpy(it.src, info->mac, 6);
    it.rssi = info->rx_ctrl.rssi;
    it.noise = (int8_t)info->rx_ctrl.noise_floor;
    it.channel = info->rx_ctrl.channel;
    it.flags = info->first_word_invalid ? 1 : 0;
    it.ts = info->rx_ctrl.timestamp;
    it.len = info->len < CSI_BYTES ? info->len : CSI_BYTES;
    memcpy(it.buf, info->buf, it.len);
    if (xQueueSend(s_queue, &it, 0) != pdTRUE) {
        s_dropped++;
    }
}

static void start_csi(void)
{
#if CONFIG_SOC_WIFI_HE_SUPPORT
    wifi_csi_config_t csi = {0};
    csi.enable = 1;
    csi.acquire_csi_legacy = 1;
  #if CONFIG_SOC_WIFI_MAC_VERSION_NUM == 3
    csi.acquire_csi_force_lltf = 1;   /* same 64-tone L-LTF from every frame format (ESP32-C5) */
  #else
    csi.acquire_csi_ht20 = 1;         /* ESP32-C6: the hub locks each link to one format */
    csi.acquire_csi_su = 1;
  #endif
#else
    wifi_csi_config_t csi = {
        .lltf_en = true,
        .htltf_en = false,
        .stbc_htltf2_en = false,
        .ltf_merge_en = true,
        .channel_filter_en = false,   /* keep subcarriers independent */
        .manu_scale = false,
        .shift = 0,
    };
#endif
    wifi_promiscuous_filter_t filter = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

/* ------------------------------------------------------------- packets */

static size_t put_header(uint8_t *p, uint8_t type)
{
    memcpy(p, "SRS1", 4);
    p[4] = type;
    p[5] = 1;
    memcpy(p + 6, s_self, 6);
    return 12;
}

static void send_to_hub(const uint8_t *buf, size_t len)
{
    if (s_sock < 0) {
        return;
    }
    sendto(s_sock, buf, len, 0, (struct sockaddr *)&s_hub, sizeof(s_hub));
}

static void send_csi(const csi_item_t *it)
{
    uint8_t p[12 + 20 + CSI_BYTES];
    size_t n = put_header(p, T_CSI);
    memcpy(p + n, it->src, 6); n += 6;
    p[n++] = (uint8_t)it->rssi;
    p[n++] = (uint8_t)it->noise;
    p[n++] = it->channel;
    p[n++] = it->flags;
    uint32_t seq = s_seq++;
    memcpy(p + n, &seq, 4); n += 4;
    memcpy(p + n, &it->ts, 4); n += 4;
    memcpy(p + n, &it->len, 2); n += 2;
    memcpy(p + n, it->buf, it->len); n += it->len;
    send_to_hub(p, n);
}

static void send_hello(void)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
        return;
    }
    uint8_t p[12 + 20];
    size_t n = put_header(p, T_HELLO);
    memcpy(p + n, ap.bssid, 6); n += 6;
    p[n++] = ap.primary;
    p[n++] = (uint8_t)ap.rssi;
    uint32_t up = (uint32_t)(esp_timer_get_time() / 1000000);
    memcpy(p + n, &up, 4); n += 4;
    char fw[8] = {0};
    strncpy(fw, FW_VERSION, sizeof(fw));
    memcpy(p + n, fw, 8); n += 8;
    send_to_hub(p, n);
}

static void handle_peers(const uint8_t *buf, int len, const struct sockaddr_in *from)
{
    if (len < 13 || memcmp(buf, "SRS1", 4) != 0 || buf[4] != T_PEERS) {
        return;
    }
    int count = buf[12];
    if (count > MAX_PEERS || len < 13 + count * 6) {
        return;
    }
    wifi_ap_record_t ap;
    bool have_ap = esp_wifi_sta_get_ap_info(&ap) == ESP_OK;

    portENTER_CRITICAL(&s_lock);
    int n = 0;
    if (have_ap) {
        memcpy(s_sources[n++].mac, ap.bssid, 6);
    }
    for (int i = 0; i < count; i++) {
        const uint8_t *m = buf + 13 + i * 6;
        if (memcmp(m, s_self, 6) == 0 || (have_ap && memcmp(m, ap.bssid, 6) == 0)) {
            continue;
        }
        memcpy(s_sources[n].mac, m, 6);
        s_sources[n].last_ts = 0;
        n++;
    }
    s_nsources = n;
    portEXIT_CRITICAL(&s_lock);

    if (!s_hub_known || s_hub.sin_addr.s_addr != from->sin_addr.s_addr) {
        ESP_LOGI(TAG, "hub at %s, %d sources", inet_ntoa(from->sin_addr), n);
    }
    s_hub = *from;
    s_hub_known = true;
    s_hub_heard_us = esp_timer_get_time();
}

static void open_socket(void)
{
    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    int yes = 1;
    setsockopt(s_sock, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes));
    struct timeval tv = { .tv_sec = 0, .tv_usec = 20000 };
    setsockopt(s_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    memset(&s_hub, 0, sizeof(s_hub));
    s_hub.sin_family = AF_INET;
    s_hub.sin_port = htons(CONFIG_SARS_HUB_PORT);
    if (strlen(CONFIG_SARS_HUB_IP) > 0) {
        inet_aton(CONFIG_SARS_HUB_IP, &s_hub.sin_addr);
        s_hub_known = true;
        s_hub_heard_us = esp_timer_get_time();
    } else {
        s_hub.sin_addr.s_addr = htonl(INADDR_BROADCAST);
        s_hub_known = false;
    }
}

static void net_task(void *arg)
{
    xEventGroupWaitBits(s_events, GOT_IP_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    open_socket();
    int64_t last_hello = 0, last_stats = 0;
    csi_item_t it;
    uint8_t rx[16 + MAX_PEERS * 6];

    for (;;) {
        /* drain queued CSI; only once we know where the hub is */
        while (xQueueReceive(s_queue, &it, pdMS_TO_TICKS(10)) == pdTRUE) {
            if (s_hub_known) {
                send_csi(&it);
            }
        }

        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        int n = recvfrom(s_sock, rx, sizeof(rx), 0, (struct sockaddr *)&from, &fl);
        if (n > 0) {
            handle_peers(rx, n, &from);
        }

        int64_t now = esp_timer_get_time();
        if (s_hub_known && strlen(CONFIG_SARS_HUB_IP) == 0 && now - s_hub_heard_us > HUB_TIMEOUT_US) {
            ESP_LOGW(TAG, "hub silent, searching again");
            s_hub.sin_addr.s_addr = htonl(INADDR_BROADCAST);
            s_hub.sin_port = htons(CONFIG_SARS_HUB_PORT);
            s_hub_known = false;
        }
        if (now - last_hello > 5000000) {
            last_hello = now;
            if ((xEventGroupGetBits(s_events) & GOT_IP_BIT) != 0) {
                send_hello();
            }
        }
        if (now - last_stats > 60000000) {
            last_stats = now;
            ESP_LOGI(TAG, "sent %" PRIu32 " reports, dropped %" PRIu32 ", free heap %" PRIu32,
                     s_seq, s_dropped, esp_get_free_heap_size());
        }
    }
}

/* ------------------------------------------------------------- Wi-Fi */

static void start_ping(const esp_netif_ip_info_t *ip)
{
#if CONFIG_SARS_PING_GATEWAY
    if (s_ping) {
        esp_ping_stop(s_ping);
        esp_ping_delete_session(s_ping);
        s_ping = NULL;
    }
    esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
    ip_2_ip4(&cfg.target_addr)->addr = ip->gw.addr;
    IP_SET_TYPE_VAL(cfg.target_addr, IPADDR_TYPE_V4);
    cfg.count = ESP_PING_COUNT_INFINITE;
    cfg.interval_ms = 1000 / CONFIG_SARS_TX_HZ;
    cfg.timeout_ms = 1000;
    cfg.data_size = 8;
    esp_ping_callbacks_t cbs = {0};
    if (esp_ping_new_session(&cfg, &cbs, &s_ping) == ESP_OK) {
        esp_ping_start(s_ping);
    } else {
        ESP_LOGE(TAG, "could not start ping session");
    }
#endif
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        wifi_event_sta_connected_t *e = data;
        portENTER_CRITICAL(&s_lock);
        memcpy(s_sources[0].mac, e->bssid, 6);
        s_sources[0].last_ts = 0;
        if (s_nsources == 0) {
            s_nsources = 1;
        }
        portEXIT_CRITICAL(&s_lock);
        ESP_LOGI(TAG, "joined %s on channel %d", (char *)e->ssid, e->channel);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, GOT_IP_BIT);
        portENTER_CRITICAL(&s_lock);
        s_nsources = 0;
        portEXIT_CRITICAL(&s_lock);
        ESP_LOGW(TAG, "disconnected, retrying");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "address " IPSTR ", router " IPSTR, IP2STR(&e->ip_info.ip), IP2STR(&e->ip_info.gw));
        start_ping(&e->ip_info);
        xEventGroupSetBits(s_events, GOT_IP_BIT);
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    s_queue = xQueueCreate(QUEUE_LEN, sizeof(csi_item_t));
    s_events = xEventGroupCreate();
    s_min_gap_us = 1000000u / (uint32_t)CONFIG_SARS_MAX_HZ;

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, CONFIG_SARS_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_SARS_WIFI_PASSWORD, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = strlen(CONFIG_SARS_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    esp_wifi_get_mac(WIFI_IF_STA, s_self);
    ESP_LOGI(TAG, "node " MACSTR " firmware " FW_VERSION, MAC2STR(s_self));
    start_csi();

    xTaskCreate(net_task, "sars_net", 4096, NULL, 5, NULL);
}
