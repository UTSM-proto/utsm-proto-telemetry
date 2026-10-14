#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>

// =====================
// Board LED
// HW-466AB ESP32-C3 SuperMini onboard LED is GPIO8, usually active-low
// =====================
#define LED_PIN 8
#define LED_ON  LOW
#define LED_OFF HIGH

// =====================
// ESP-NOW settings
// =====================
static const uint8_t ESPNOW_CHANNEL = 1;

// Replace this with your OLED HUD ESP32 MAC address.
uint8_t HUD_MAC[] = {0xAC, 0xA7, 0x04, 0xD3, 0x32, 0x78};

// =====================
// Shared packet format
// Must match receiver exactly.
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

// =====================
// Demo table
// This simulates real collected/computed telemetry.
// In the real car, these values come from sensors, GPS, BMS, and your coach table.
// =====================
struct DemoFrame_t {
  uint16_t speed_dkph;
  int32_t voltage_mV;
  int32_t current_mA;
  uint8_t soc_percent;
  uint8_t gps_status;
  uint8_t satellites;
  uint8_t hdop_x10;
  uint8_t coach_action;
  uint8_t coach_confidence_percent;
  uint8_t fault_flags;
};

const DemoFrame_t demoTable[] = {
  // speed  voltage  current  soc gps        sats hdop coach          conf fault
  { 285,    48700,   4200,    84, GPS_FIX,   9,   12,  COACH_COAST,   91,  0 },
  { 292,    48600,   4600,    84, GPS_FIX,   9,   12,  COACH_COAST,   91,  0 },
  { 300,    48500,   5200,    84, GPS_FIX,   9,   13,  COACH_HOLD,    88,  0 },
  { 310,    48300,   7200,    84, GPS_FIX,   8,   13,  COACH_HOLD,    87,  0 },
  { 318,    48100,   10500,   83, GPS_FIX,   8,   14,  COACH_PULSE,   86,  0 },
  { 326,    47900,   13200,   83, GPS_FIX,   8,   14,  COACH_PULSE,   84,  0 },
  { 333,    47700,   15500,   83, GPS_FIX,   7,   16,  COACH_BACK_OFF,82,  0 },
  { 328,    47800,   9800,    83, GPS_FIX,   8,   15,  COACH_HOLD,    86,  0 },
  { 317,    48000,   6100,    83, GPS_FIX,   9,   13,  COACH_COAST,   90,  0 },
  { 305,    48200,   3900,    83, GPS_FIX,   9,   12,  COACH_COAST,   92,  0 },
};

const size_t DEMO_TABLE_LEN = sizeof(demoTable) / sizeof(demoTable[0]);

uint16_t sequenceNumber = 0;
size_t demoIndex = 0;

// =====================
// ESP-NOW setup
// =====================
bool initEspNow() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  Serial.println();
  Serial.println("UTSM Clean Telemetry Sender");
  Serial.print("Sender MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    return false;
  }

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, HUD_MAC, 6);
  peerInfo.channel = ESPNOW_CHANNEL;
  peerInfo.encrypt = false;

  esp_err_t addStatus = esp_now_add_peer(&peerInfo);

  if (addStatus != ESP_OK && addStatus != ESP_ERR_ESPNOW_EXIST) {
    Serial.print("Failed to add HUD peer. Error: ");
    Serial.println(addStatus);
    return false;
  }

  Serial.println("ESP-NOW sender ready");
  return true;
}

// =====================
// Build packet from table row
// =====================
HudPacket_t buildPacketFromDemoFrame(const DemoFrame_t &f) {
  HudPacket_t p = {};

  p.packet_version = 1;
  p.timestamp_ms = millis();
  p.sequence = sequenceNumber++;

  p.gps_status = f.gps_status;
  p.satellites = f.satellites;
  p.hdop_x10 = f.hdop_x10;

  p.speed_dkph = f.speed_dkph;

  p.voltage_mV = f.voltage_mV;
  p.current_mA = f.current_mA;

  // Power is the only derived value here.
  // mV * mA = microwatts, so divide by 1000 to get milliwatts.
  p.power_mW = (int32_t)(((int64_t)f.voltage_mV * (int64_t)f.current_mA) / 1000);

  p.soc_percent = f.soc_percent;

  p.coach_action = f.coach_action;
  p.coach_confidence_percent = f.coach_confidence_percent;

  p.fault_flags = f.fault_flags;

  return p;
}

// =====================
// Setup
// =====================
void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LED_OFF);

  if (!initEspNow()) {
    digitalWrite(LED_PIN, LED_ON);
    while (true) delay(1000);
  }
}

// =====================
// Loop
// =====================
void loop() {
  static uint32_t lastSendMs = 0;
  static bool ledState = false;

  if (millis() - lastSendMs >= 150) {
    lastSendMs = millis();

    const DemoFrame_t &frame = demoTable[demoIndex];
    demoIndex = (demoIndex + 1) % DEMO_TABLE_LEN;

    HudPacket_t pkt = buildPacketFromDemoFrame(frame);

    esp_err_t result = esp_now_send(
      HUD_MAC,
      (uint8_t *)&pkt,
      sizeof(pkt)
    );

    if (result == ESP_OK) {
      ledState = !ledState;
      digitalWrite(LED_PIN, ledState ? LED_ON : LED_OFF);

      Serial.print("TX seq=");
      Serial.print(pkt.sequence);
      Serial.print(" speed=");
      Serial.print(pkt.speed_dkph / 10.0f);
      Serial.print(" V=");
      Serial.print(pkt.voltage_mV / 1000.0f);
      Serial.print(" I=");
      Serial.print(pkt.current_mA / 1000.0f);
      Serial.print(" P=");
      Serial.print(pkt.power_mW / 1000.0f);
      Serial.print(" coach=");
      Serial.println(pkt.coach_action);
    } else {
      digitalWrite(LED_PIN, LED_ON);
      Serial.print("ESP-NOW send failed. Error: ");
      Serial.println(result);
    }
  }
}
