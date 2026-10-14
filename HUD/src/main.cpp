#include <Arduino.h>
#include <U8g2lib.h>
#include <SPI.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_arduino_version.h>
#include <string.h>

// =====================
// OLED pin mapping
// =====================
#define OLED_CLK   4
#define OLED_MOSI  6
#define OLED_CS    7
#define OLED_DC    2
#define OLED_RST   3

U8G2_SSD1309_128X64_NONAME0_F_4W_SW_SPI u8g2(
  U8G2_R0,
  OLED_CLK,
  OLED_MOSI,
  OLED_CS,
  OLED_DC,
  OLED_RST
);

// =====================
// Board LED
// =====================
#define LED_PIN 8
#define LED_ON  LOW
#define LED_OFF HIGH

// =====================
// ESP-NOW settings
// =====================
static const uint8_t ESPNOW_CHANNEL = 1;

const bool USE_SENDER_FILTER = true;

uint8_t EXPECTED_SENDER_MAC[] = {0xAC, 0xA7, 0x04, 0xD8, 0x65, 0x0C};
// =====================
// Shared packet format
// Must match sender exactly.
// =====================
enum CoachAction : uint8_t {
  COACH_COAST = 0,
  COACH_HOLD = 1,
  COACH_PULSE = 2,
  COACH_BACK_OFF = 3,
  COACH_FAULT = 4
};

enum GpsStatus : uint8_t {
  GPS_NO_DATA = 0,
  GPS_NO_FIX = 1,
  GPS_FIX = 2
};

struct HudPacket_t {
  uint8_t packet_version;
  uint32_t timestamp_ms;
  uint16_t sequence;

  uint8_t gps_status;
  uint8_t satellites;
  uint8_t hdop_x10;

  uint16_t speed_dkph;

  int32_t voltage_mV;
  int32_t current_mA;
  int32_t power_mW;

  uint8_t soc_percent;

  uint8_t coach_action;
  uint8_t coach_confidence_percent;

  uint8_t fault_flags;
} __attribute__((packed));

HudPacket_t latestPacket = {};
volatile bool newPacket = false;
uint32_t lastReceiveMs = 0;
uint16_t lastShownSeq = 0xFFFF;

// =====================
// Helpers
// =====================
const char *coachText(uint8_t action) {
  switch (action) {
    case COACH_COAST:    return "COAST";
    case COACH_HOLD:     return "HOLD";
    case COACH_PULSE:    return "PULSE";
    case COACH_BACK_OFF: return "BACK OFF";
    case COACH_FAULT:    return "FAULT";
    default:             return "UNKNOWN";
  }
}

const char *gpsText(uint8_t status) {
  switch (status) {
    case GPS_FIX:     return "GPS";
    case GPS_NO_FIX:  return "NOFIX";
    case GPS_NO_DATA: return "NOGPS";
    default:          return "GPS?";
  }
}

bool macMatches(const uint8_t *a, const uint8_t *b) {
  return memcmp(a, b, 6) == 0;
}

// =====================
// ESP-NOW receive callback
// =====================
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
void onDataRecv(const esp_now_recv_info_t *recvInfo, const uint8_t *data, int len)
{
  const uint8_t *senderMac = recvInfo->src_addr;
#else
void onDataRecv(const uint8_t *senderMac, const uint8_t *data, int len)
{
#endif
  if (USE_SENDER_FILTER && !macMatches(senderMac, EXPECTED_SENDER_MAC)) {
    return;
  }

  if (len != sizeof(HudPacket_t)) {
    return;
  }

  HudPacket_t pkt;
  memcpy(&pkt, data, sizeof(pkt));

  if (pkt.packet_version != 1) {
    return;
  }

  latestPacket = pkt;
  lastReceiveMs = millis();
  newPacket = true;

  static bool ledState = false;
  ledState = !ledState;
  digitalWrite(LED_PIN, ledState ? LED_ON : LED_OFF);
}

// =====================
// OLED screens
// =====================
void drawWaitingScreen() {
  u8g2.clearBuffer();

  u8g2.setFont(u8g2_font_7x14B_tf);
  u8g2.drawStr(8, 22, "UTSM HUD");

  u8g2.setFont(u8g2_font_6x12_tf);
  u8g2.drawStr(0, 42, "Waiting for ESP-NOW");
  u8g2.drawStr(0, 58, "telemetry packets");

  u8g2.sendBuffer();
}

void drawNoDataScreen() {
  u8g2.clearBuffer();

  u8g2.setFont(u8g2_font_7x14B_tf);
  u8g2.drawStr(28, 24, "NO DATA");

  u8g2.setFont(u8g2_font_6x12_tf);
  u8g2.drawStr(18, 44, "Telemetry lost");

  u8g2.sendBuffer();
}

void drawFaultScreen(const HudPacket_t &p) {
  u8g2.clearBuffer();

  u8g2.setFont(u8g2_font_7x14B_tf);
  u8g2.drawStr(36, 22, "FAULT");

  u8g2.setFont(u8g2_font_6x12_tf);

  if (p.fault_flags & 0x01) {
    u8g2.drawStr(0, 42, "LOW VOLTAGE");
  } else if (p.fault_flags & 0x02) {
    u8g2.drawStr(0, 42, "HIGH CURRENT");
  } else if (p.fault_flags & 0x04) {
    u8g2.drawStr(0, 42, "GPS FAULT");
  } else {
    u8g2.drawStr(0, 42, "Check telemetry");
  }

  u8g2.sendBuffer();
}

void drawHud(const HudPacket_t &p) {
  if (p.fault_flags != 0 || p.coach_action == COACH_FAULT) {
    drawFaultScreen(p);
    return;
  }

  char buf[32];

  float speedKph = p.speed_dkph / 10.0f;
  float voltage = p.voltage_mV / 1000.0f;
  float current = p.current_mA / 1000.0f;
  int powerW = p.power_mW / 1000;

  u8g2.clearBuffer();

  // Top status row
  u8g2.setFont(u8g2_font_5x8_tf);

  u8g2.drawStr(0, 8, "LINK");
  u8g2.drawStr(30, 8, gpsText(p.gps_status));

  snprintf(buf, sizeof(buf), "S%u", p.satellites);
  u8g2.drawStr(63, 8, buf);

  snprintf(buf, sizeof(buf), "SOC%u%%", p.soc_percent);
  u8g2.drawStr(88, 8, buf);

  // Big speed
  u8g2.setFont(u8g2_font_logisoso32_tf);
  snprintf(buf, sizeof(buf), "%2.0f", speedKph);
  u8g2.drawStr(18, 43, buf);

  u8g2.setFont(u8g2_font_6x12_tf);
  u8g2.drawStr(82, 43, "km/h");

  // Voltage/current/power row
  u8g2.setFont(u8g2_font_5x8_tf);
  snprintf(buf, sizeof(buf), "%.1fV %.1fA %dW", voltage, current, powerW);
  u8g2.drawStr(0, 55, buf);

  // Coach row
  u8g2.setFont(u8g2_font_7x14B_tf);
  u8g2.drawStr(0, 64, coachText(p.coach_action));

  snprintf(buf, sizeof(buf), "%u%%", p.coach_confidence_percent);
  u8g2.setFont(u8g2_font_5x8_tf);
  u8g2.drawStr(104, 64, buf);

  u8g2.sendBuffer();
}

// =====================
// Setup
// =====================
void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LED_OFF);

  u8g2.begin();
  u8g2.setContrast(255);
  drawWaitingScreen();

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  Serial.println();
  Serial.println("UTSM OLED ESP-NOW Receiver");
  Serial.print("HUD MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    drawNoDataScreen();
    while (true) delay(1000);
  }

  esp_now_register_recv_cb(onDataRecv);

  Serial.println("Receiver ready.");
}

// =====================
// Loop
// =====================
void loop() {
  static uint32_t lastDrawMs = 0;

  if (millis() - lastDrawMs >= 120) {
    lastDrawMs = millis();

    bool stale = millis() - lastReceiveMs > 1000;

    if (lastReceiveMs == 0) {
      drawWaitingScreen();
    } else if (stale) {
      drawNoDataScreen();
    } else {
      drawHud(latestPacket);

      if (latestPacket.sequence != lastShownSeq) {
        lastShownSeq = latestPacket.sequence;

        Serial.print("RX seq=");
        Serial.print(latestPacket.sequence);
        Serial.print(" speed=");
        Serial.print(latestPacket.speed_dkph / 10.0f);
        Serial.print(" V=");
        Serial.print(latestPacket.voltage_mV / 1000.0f);
        Serial.print(" I=");
        Serial.print(latestPacket.current_mA / 1000.0f);
        Serial.print(" P=");
        Serial.print(latestPacket.power_mW / 1000.0f);
        Serial.print(" coach=");
        Serial.println(latestPacket.coach_action);
      }
    }

    newPacket = false;
  }
}