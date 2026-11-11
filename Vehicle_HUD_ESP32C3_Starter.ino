/*
  VEHICLE HUD - ESP32-C3 Super Mini
  Target: Arduino IDE
  Initial display target: 128x64 monochrome I2C OLED (SSD1306)

  LIBRARIES (Arduino IDE > Library Manager):
    1. Adafruit GFX Library
    2. Adafruit SSD1306

  IMPORTANT:
    - 128x64 is a resolution, NOT a colour specification.
    - This starter sketch assumes a monochrome SSD1306 I2C OLED.
    - Confirm the actual OLED controller, interface, I2C address and pins
      before connecting hardware.
    - The default ESP32-C3 Super Mini pin choices below are placeholders.
    - Vehicle inputs are SIMULATED. Do not use this as a safety-critical
      driver display until the real data path and failure behaviour are tested.
*/

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ================================================================
// 1. USER CONFIGURATION - CHANGE THESE WHEN HARDWARE IS KNOWN
// ================================================================

// ---------- Display ----------
#define DISPLAY_WIDTH       128
#define DISPLAY_HEIGHT      64
#define OLED_I2C_ADDRESS    0x3C

// PLACEHOLDERS ONLY. Verify the pinout of your particular C3 board.
// Common ESP32-C3 Super Mini examples use SDA=8 and SCL=9, but variants exist.
#define OLED_SDA_PIN        8
#define OLED_SCL_PIN        9

// Set to 1 to rotate the screen 180 degrees.
#define DISPLAY_ROTATION_180 0

// Display refresh interval (milliseconds).
#define DISPLAY_REFRESH_MS  50

// ---------- Data source ----------
// For now, use simulated values.
// Later, replace this with UART, CAN, I2C, sensor inputs, etc.
#define USE_SIMULATED_DATA  1

// ---------- Units ----------
#define POWER_DISPLAY_IN_KW 1
#define SPEED_DISPLAY_IN_KMH 1

// ---------- Display formatting ----------
#define POWER_DECIMAL_PLACES 1
#define TEMP_DECIMAL_PLACES  1

// ---------- Strategy indicator configuration ----------
// The levels are placeholders. Define what they mean with your team later.
#define MAX_STRATEGY_LEVEL 4

// ---------- Simulation ----------
#define SIMULATION_UPDATE_MS 250

// ================================================================
// 2. DATA STRUCTURES
// ================================================================

struct VehicleData {
  float power_W;          // Instantaneous electrical power, watts
  float speed_mps;        // Vehicle speed, metres per second
  float batteryTemp_C;    // Battery temperature, degrees Celsius
  float current_A;        // Current draw, amperes

  // Strategy levels: 0 = none, MAX_STRATEGY_LEVEL = all chevrons.
  // These are visual commands for now, NOT calculated throttle advice.
  uint8_t greenLevel;
  uint8_t redLevel;

  bool dataValid;
};

VehicleData vehicle = {
  0.0f,  // power_W
  0.0f,  // speed_mps
  0.0f,  // batteryTemp_C
  0.0f,  // current_A
  0,     // greenLevel
  0,     // redLevel
  false  // dataValid
};

// ================================================================
// 3. DISPLAY OBJECT
// ================================================================

Adafruit_SSD1306 display(
  DISPLAY_WIDTH,
  DISPLAY_HEIGHT,
  &Wire,
  -1  // No reset pin; change if your OLED has a dedicated reset pin.
);

// ================================================================
// 4. TIMING
// ================================================================

unsigned long lastDisplayUpdate = 0;
unsigned long lastDataUpdate = 0;

// ================================================================
// 5. DATA INPUT
// ================================================================

void updateVehicleData() {
#if USE_SIMULATED_DATA

  // --------------------------------------------------------------
  // SIMULATED INPUTS
  // Replace this section when the real communication method is known.
  // --------------------------------------------------------------

  static float simulatedTime = 0.0f;
  simulatedTime += 0.25f;

  vehicle.power_W = 12400.0f + 3000.0f * sin(simulatedTime * 0.5f);
  vehicle.speed_mps = 15.0f + 5.0f * sin(simulatedTime * 0.25f);
  vehicle.batteryTemp_C = 27.0f + 2.0f * sin(simulatedTime * 0.1f);
  vehicle.current_A = 42.0f + 10.0f * sin(simulatedTime * 0.4f);

  // Demonstration only: cycle through indicator levels.
  // Replace with your eventual strategy output.
  vehicle.greenLevel = (uint8_t)(2 + (1 + sin(simulatedTime * 0.3f)) * 1.0f);
  vehicle.redLevel = (uint8_t)(1 + (1 + sin(simulatedTime * 0.2f)) * 1.0f);

  vehicle.dataValid = true;

#else

  // --------------------------------------------------------------
  // REAL DATA INPUT PLACEHOLDER
  // Choose and implement ONE or more methods once the vehicle
  // electronics are known:
  //
  //   - UART: Serial1 / HardwareSerial
  //   - CAN: an external CAN controller/transceiver or supported
  //          ESP32-C3 CAN/TWAI solution, subject to hardware support
  //   - I2C: Wire
  //   - Direct sensors: GPIO / ADC / pulse counting
  //
  // Parse incoming data, validate it, apply units, and then assign:
  //
  // vehicle.power_W       = receivedPowerWatts;
  // vehicle.speed_mps     = receivedSpeedMetresPerSecond;
  // vehicle.batteryTemp_C = receivedBatteryTemperatureC;
  // vehicle.current_A     = receivedCurrentAmps;
  // vehicle.greenLevel    = receivedGreenLevel;
  // vehicle.redLevel      = receivedRedLevel;
  // vehicle.dataValid     = true;  // only after valid data is received
  //
  // Do not set dataValid=true just because the ESP32 has booted.
  // Add a communication timeout so stale values are not shown as current.

  vehicle.dataValid = false;

#endif

  // Keep strategy levels inside the permitted range.
  vehicle.greenLevel = constrain(vehicle.greenLevel, 0, MAX_STRATEGY_LEVEL);
  vehicle.redLevel = constrain(vehicle.redLevel, 0, MAX_STRATEGY_LEVEL);
}

// ================================================================
// 6. UNIT CONVERSIONS / FORMATTING
// ================================================================

float getPowerDisplayValue() {
#if POWER_DISPLAY_IN_KW
  return vehicle.power_W / 1000.0f;
#else
  return vehicle.power_W;
#endif
}

float getSpeedDisplayValue() {
#if SPEED_DISPLAY_IN_KMH
  return vehicle.speed_mps * 3.6f;
#else
  return vehicle.speed_mps;
#endif
}

const char* getPowerUnit() {
#if POWER_DISPLAY_IN_KW
  return "kW";
#else
  return "W";
#endif
}

const char* getSpeedUnit() {
#if SPEED_DISPLAY_IN_KMH
  return "km/h";
#else
  return "m/s";
#endif
}

// ================================================================
// 7. GRAPHICS HELPERS
// ================================================================

void drawLightningBolt(int x, int y) {
  // Small monochrome lightning bolt.
  display.fillTriangle(
    x + 7, y,
    x + 1, y + 11,
    x + 7, y + 11,
    SSD1306_WHITE
  );
  display.fillTriangle(
    x + 7, y + 7,
    x + 3, y + 21,
    x + 13, y + 7,
    SSD1306_WHITE
  );
}

void drawThermometer(int x, int y) {
  display.drawRoundRect(x + 4, y, 5, 17, 2, SSD1306_WHITE);
  display.drawCircle(x + 6, y + 19, 5, SSD1306_WHITE);
  display.fillCircle(x + 6, y + 19, 3, SSD1306_WHITE);
  display.drawLine(x + 6, y + 4, x + 6, y + 17, SSD1306_WHITE);
}

void drawChevron(int centerX, int y, int width, int height, bool pointsUp) {
  // Draws one outlined-looking, thick monochrome chevron.
  // Direction and count distinguish the two strategy groups on B/W OLEDs.
  int half = width / 2;
  int thickness = 3;

  if (pointsUp) {
    display.fillTriangle(
      centerX - half, y + height / 2,
      centerX, y,
      centerX + half, y + height / 2,
      SSD1306_WHITE
    );
    display.fillRect(centerX - half, y + height / 2 - thickness,
                     width, thickness, SSD1306_BLACK);
  } else {
    display.fillTriangle(
      centerX - half, y,
      centerX + half, y,
      centerX, y + height / 2,
      SSD1306_WHITE
    );
    display.fillRect(centerX - half, y, width, thickness, SSD1306_BLACK);
  }
}

void drawStrategyIndicators() {
  // Right-hand column. Up = green strategy in the colour design;
  // down = red strategy. A monochrome OLED uses direction + position.
  const int centerX = 108;
  const int chevronWidth = 22;
  const int chevronHeight = 8;
  const int gap = 2;

  // Upward chevrons, top half.
  for (int i = 0; i < vehicle.greenLevel; i++) {
    int y = 2 + i * (chevronHeight + gap);
    drawChevron(centerX, y, chevronWidth, chevronHeight, true);
  }

  // Downward chevrons, bottom half.
  for (int i = 0; i < vehicle.redLevel; i++) {
    int y = 39 + i * (chevronHeight + gap);
    drawChevron(centerX, y, chevronWidth, chevronHeight, false);
  }
}

// ================================================================
// 8. HUD DRAWING
// ================================================================

void drawPower() {
  drawLightningBolt(2, 2);

  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(18, 3);
  display.print("PWR");

  display.setTextSize(1);
  display.setCursor(48, 3);
  display.print(getPowerDisplayValue(), POWER_DECIMAL_PLACES);
  display.print(" ");
  display.print(getPowerUnit());
}

void drawSpeed() {
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(2, 22);

  float speed = getSpeedDisplayValue();
  if (speed < 0) speed = 0;

  // Fixed-width style: pad values to three digits.
  if (speed < 100) display.print("0");
  if (speed < 10) display.print("0");
  display.print((int)round(speed));

  display.setTextSize(1);
  display.setCursor(2, 40);
  display.print(getSpeedUnit());
}

void drawCurrent() {
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(65, 22);

  float amps = vehicle.current_A;
  if (amps < 0) amps = 0;

  if (amps < 100) display.print("0");
  if (amps < 10) display.print("0");
  display.print((int)round(amps));

  display.setTextSize(1);
  display.setCursor(65, 40);
  display.print("A");
}

void drawTemperature() {
  drawThermometer(2, 42);

  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(17, 48);
  display.print("TEMP");

  display.setTextSize(1);
  display.setCursor(17, 57);
  display.print(vehicle.batteryTemp_C, TEMP_DECIMAL_PLACES);
  display.print(" C");
}

void drawInvalidDataWarning() {
  if (!vehicle.dataValid) {
    display.fillRect(0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT, SSD1306_BLACK);
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(18, 19);
    display.print("NO DATA");
    display.setCursor(7, 34);
    display.print("CHECK CONNECTION");
  }
}

void drawHUD() {
  display.clearDisplay();

  if (!vehicle.dataValid) {
    drawInvalidDataWarning();
  } else {
    drawPower();
    drawSpeed();
    drawCurrent();
    drawTemperature();
    drawStrategyIndicators();
  }

  display.display();
}

// ================================================================
// 9. SETUP
// ================================================================

void setup() {
  Serial.begin(115200);
  delay(100);

  // Initialize I2C using the configured pins.
  Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN);

  // Initialize OLED.
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDRESS)) {
    Serial.println("OLED initialization failed. Check wiring/address/controller.");
    while (true) {
      delay(1000);
    }
  }

#if DISPLAY_ROTATION_180
  display.setRotation(2);
#else
  display.setRotation(0);
#endif

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextWrap(false);
  display.display();

  Serial.println("Vehicle HUD started.");
}

// ================================================================
// 10. MAIN LOOP
// ================================================================

void loop() {
  unsigned long now = millis();

  if (now - lastDataUpdate >= SIMULATION_UPDATE_MS) {
    lastDataUpdate = now;
    updateVehicleData();
  }

  if (now - lastDisplayUpdate >= DISPLAY_REFRESH_MS) {
    lastDisplayUpdate = now;
    drawHUD();
  }
}
