// ============================================================
//  Bio Environmental Handheld Monitoring System
//  Sensors : 2x DHT11, MAX30102 (HR + SpO2)
//  Display : 0.96" SSD1306 OLED (128x64, I2C)
//  Board   : ESP32
//
//  Offline logging: readings saved to LittleFS when WiFi is
//  unavailable, then forwarded automatically on reconnect.
// ============================================================

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "MAX30105.h"
#include "heartRate.h"
#include "spo2_algorithm.h"
#include "DHT.h"

// ── Networking ───────────────────────────────────────────────
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "time.h"

// ── Deep Sleep ───────────────────────────────────────────────
#include "esp_sleep.h"
#include "driver/rtc_io.h"

// ── Offline Logging ──────────────────────────────────────────
#include <LittleFS.h>
#define LOG_FILE "/offline_log.jsonl"

// ── I2C Pin Definitions ──────────────────────────────────────
#define OLED_SDA 21
#define OLED_SCL 22
#define MAX_SDA  32
#define MAX_SCL  33

// ── Button Pin ───────────────────────────────────────────────
#define BUTTON_PIN 15

// ── OLED ─────────────────────────────────────────────────────
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT  64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ── Sensor Pins ──────────────────────────────────────────────
#define DHTPIN1  4
#define DHTPIN2  5
#define DHTTYPE  DHT11

DHT      dht1(DHTPIN1, DHTTYPE);
DHT      dht2(DHTPIN2, DHTTYPE);
MAX30105 particleSensor;

// ── Heart Rate Variables ─────────────────────────────────────
long  lastBeat      = 0;
float bpm           = 0;
int   beatAvg       = 0;

const byte RATE_SIZE = 15;
byte  rates[RATE_SIZE];
byte  rateSpot       = 0;
byte  validRateCount = 0;

const int MIN_BPM = 40;
const int MAX_BPM = 180;

// ── Finger Detection (hysteresis) ────────────────────────────
const long FINGER_ON_THRESHOLD  = 35000;
const long FINGER_OFF_THRESHOLD = 30000;

// ── Timing & State ───────────────────────────────────────────
float avgTemp     = 0.0;
float avgHumidity = 0.0;

enum SystemState {
  PASSIVE,
  WAITING_FOR_FINGER,
  MEASURING,
  CALCULATING_SPO2,
  RESULTS_READY
};
SystemState currentState = PASSIVE;

unsigned long measurementStartTime = 0;
unsigned long lastSecondTick       = 0;
int           timeLeft             = 30;

unsigned long resultDisplayStart        = 0;
const unsigned long RESULT_DISPLAY_DURATION = 10000; // 10 s

const uint64_t     DEEP_SLEEP_TIME_US      = 120ULL * 1000000ULL; // 2 min
const unsigned long ENV_SCREEN_HOLD_DURATION = 3000;              // 3 s

// ── SpO2 ─────────────────────────────────────────────────────
int32_t finalSpO2   = 0;
int8_t  validSPO2   = 0;
int32_t dummyHR     = 0;
int8_t  dummyValidHR = 0;

// ── Sensor State ─────────────────────────────────────────────
bool switchState          = false;
bool lastSwitchState      = false;
bool heartSensorInitialized = false;

// ── Networking Config ────────────────────────────────────────
#define WIFI_SSID       "Plus Ultra"
#define WIFI_PASS       "Mischief Managed"
#define SERVER_URL      "http://10.186.115.225:8000/data"
#define NTP_SERVER      "pool.ntp.org"
#define GMT_OFFSET_SEC  19800L
#define DST_OFFSET_SEC  0
#define API_KEY         "98063117"

bool          wifiStarted   = false;
bool          wifiConnected = false;
bool          timeSynced    = false;
unsigned long wifiStartTime = 0;
const unsigned long WIFI_TIMEOUT = 5000; // 5 s

// ============================================================
//  LittleFS — Offline Logging
// ============================================================

void initFS() {
  if (!LittleFS.begin(true)) {   // true = format on first use
    Serial.println("[FS] LittleFS mount failed");
  } else {
    Serial.println("[FS] LittleFS mounted");
  }
}

void appendToLog(const String& jsonLine) {
  File f = LittleFS.open(LOG_FILE, FILE_APPEND);
  if (!f) {
    Serial.println("[FS] Could not open log for append");
    return;
  }
  f.println(jsonLine);
  f.close();
  Serial.println("[FS] Logged offline: " + jsonLine);
}

bool hasOfflineLog() {
  if (!LittleFS.exists(LOG_FILE)) return false;
  File f = LittleFS.open(LOG_FILE, FILE_READ);
  if (!f) return false;
  bool hasData = f.size() > 0;
  f.close();
  return hasData;
}

void flushOfflineLog() {
  if (!hasOfflineLog()) return;
  if (WiFi.status() != WL_CONNECTED) return;

  Serial.println("[FS] Flushing offline log...");

  File f = LittleFS.open(LOG_FILE, FILE_READ);
  if (!f) {
    Serial.println("[FS] Could not open log for reading");
    return;
  }

  int sent   = 0;
  int failed = 0;

  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;

    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-API-Key", API_KEY);
    int code = http.POST(line);
    http.end();

    if (code == 201) {
      sent++;
      Serial.println("[FS] Flushed: " + line);
    } else {
      failed++;
      Serial.printf("[FS] Flush failed (HTTP %d): %s\n", code, line.c_str());
    }

    delay(100);
  }

  f.close();

  if (failed == 0) {
    LittleFS.remove(LOG_FILE);
    Serial.printf("[FS] Flush complete — %d sent, log cleared\n", sent);
  } else {
    Serial.printf("[FS] Flush partial — %d sent, %d failed, log kept\n", sent, failed);
  }
}

// ============================================================
//  Networking Helpers
// ============================================================

String getTimestamp() {
  struct tm ti;
  if (!getLocalTime(&ti)) return "OFFLINE";
  char buf[25];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &ti);
  return String(buf);
}

bool postJSON(const String& payload) {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  int code = http.POST(payload);
  http.end();

  Serial.printf("[NET] POST %d : %s\n", code, payload.c_str());
  return (code == 201);
}

String buildEnvPayload(const String& ts) {
  StaticJsonDocument<256> doc;
  doc["timestamp"]   = ts;
  doc["temperature"] = avgTemp;
  doc["humidity"]    = avgHumidity;
  String payload;
  serializeJson(doc, payload);
  return payload;
}

String buildHRPayload(const String& ts) {
  StaticJsonDocument<256> doc;
  doc["timestamp"]   = ts;
  doc["temperature"] = avgTemp;
  doc["humidity"]    = avgHumidity;
  doc["heart_rate"]  = beatAvg;
  if (finalSpO2 > 80 && finalSpO2 <= 100) doc["spo2"] = (int)finalSpO2;
  String payload;
  serializeJson(doc, payload);
  return payload;
}

void sendEnvData() {
  String ts      = getTimestamp();
  String payload = buildEnvPayload(ts);

  if (WiFi.status() == WL_CONNECTED) {
    if (!postJSON(payload)) {
      // POST failed even though WiFi was up — log it
      appendToLog(payload);
    }
  } else {
    appendToLog(payload);
  }
}

void sendHRData() {
  String ts      = getTimestamp();
  String payload = buildHRPayload(ts);

  if (WiFi.status() == WL_CONNECTED) {
    if (!postJSON(payload)) {
      appendToLog(payload);
    }
  } else {
    appendToLog(payload);
  }
}

void startWiFi() {
  if (wifiStarted || WiFi.status() == WL_CONNECTED) return;

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  wifiStarted   = true;
  wifiConnected = false;
  timeSynced    = false;
  wifiStartTime = millis();
  Serial.println("[NET] WiFi starting...");
}

void handleWiFi() {
  if (!wifiStarted) return;

  if (!wifiConnected && WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    Serial.println("[NET] WiFi connected: " + WiFi.localIP().toString());
    configTime(GMT_OFFSET_SEC, DST_OFFSET_SEC, NTP_SERVER);
    timeSynced = true;

    // ── Flush any offline readings now that we're connected ──
    flushOfflineLog();
    return;
  }

  if (!wifiConnected && millis() - wifiStartTime >= WIFI_TIMEOUT) {
    Serial.println("[NET] WiFi timeout — will log offline");
    wifiStarted   = false;
    wifiConnected = false;
  }
}

// ============================================================
//  Heart Sensor Helpers
// ============================================================

bool initHeartSensor() {
  if (heartSensorInitialized) {
    particleSensor.wakeUp();
    delay(50);
    return true;
  }

  Wire1.begin(MAX_SDA, MAX_SCL);
  Wire1.setClock(400000);

  if (!particleSensor.begin(Wire1, I2C_SPEED_FAST)) {
    Serial.println("[HR] MAX30102 not found on Wire1");
    return false;
  }

  particleSensor.setup(30, 4, 2, 100, 411, 4096);
  particleSensor.setPulseAmplitudeRed(0x0A);
  particleSensor.wakeUp();
  delay(50);

  heartSensorInitialized = true;
  return true;
}

void sleepHeartSensor() {
  if (heartSensorInitialized) {
    particleSensor.shutDown();
    delay(20);
  }
}

void resetHeartMeasurementVariables() {
  beatAvg       = 0;
  bpm           = 0;
  rateSpot      = 0;
  validRateCount = 0;
  lastBeat      = 0;
  timeLeft      = 30;

  for (byte i = 0; i < RATE_SIZE; i++) rates[i] = 0;
}

int getDisplayedBPM() {
  if (validRateCount == 0) return 0;
  int sum = 0;
  for (byte i = 0; i < validRateCount; i++) sum += rates[i];
  return sum / validRateCount;
}

// ============================================================
//  OLED
// ============================================================

void drawWiFiIcon(int x, int y) {
  display.drawCircle(x + 3, y + 5, 1, WHITE);
  display.drawFastHLine(x,     y + 2, 7, WHITE);
  display.drawFastHLine(x + 1, y + 4, 5, WHITE);
}

void updateOLED() {
  display.clearDisplay();
  display.setTextColor(WHITE);

  if (currentState == PASSIVE) {
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.print("Env Monitor");

    if (WiFi.status() == WL_CONNECTED) drawWiFiIcon(115, 0);

    display.drawLine(0, 10, 127, 10, WHITE);

    display.setTextSize(2);
    display.setCursor(0, 20);
    display.print("T: ");
    if (avgTemp != -999) { display.print(avgTemp, 1); display.print("C"); }
    else display.print("Err");

    display.setCursor(0, 42);
    display.print("H: ");
    if (avgHumidity != -999) { display.print(avgHumidity, 0); display.print("%"); }
    else display.print("Err");

  } else {
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.print("Health Monitor");

    if (WiFi.status() == WL_CONNECTED) drawWiFiIcon(115, 0);

    display.drawLine(0, 10, 127, 10, WHITE);

    if (currentState == WAITING_FOR_FINGER) {
      display.setCursor(0, 20);
      display.print("Keep your finger");
      display.setCursor(0, 34);
      display.print("on the Heart Rate");
      display.setCursor(0, 48);
      display.print("Sensor");

    } else if (currentState == MEASURING) {
      display.setCursor(0, 20);
      display.print("Measuring HR");
      display.setCursor(0, 34);
      display.print("BPM: ");
      if (beatAvg > 0) display.print(beatAvg);
      else display.print("--");
      display.setCursor(0, 48);
      display.print("Time: ");
      display.print(timeLeft);
      display.print("s");

    } else if (currentState == CALCULATING_SPO2) {
      display.setCursor(0, 24);
      display.print("Calculating SpO2");
      display.setCursor(0, 40);
      display.print("Please Wait...");

    } else if (currentState == RESULTS_READY) {
      display.setCursor(0, 18);
      display.print("Scan Complete");
      display.setCursor(0, 34);
      display.print("HR: ");
      if (beatAvg > 0) display.print(beatAvg);
      else display.print("--");
      display.setCursor(0, 48);
      display.print("SpO2: ");
      if (finalSpO2 > 80 && finalSpO2 <= 100) {
        display.print(finalSpO2);
        display.print("%");
      } else {
        display.print("--");
      }
    }
  }

  display.display();
}

// ============================================================
//  DHT — averaged reading
//  BUG FIX: humidity offset applied in ALL branches
// ============================================================
const float HUMIDITY_OFFSET = -20.0;

void updateDHTAverages() {
  float t1 = dht1.readTemperature();
  float t2 = dht2.readTemperature();
  float h1 = dht1.readHumidity();
  float h2 = dht2.readHumidity();

  if      (!isnan(t1) && !isnan(t2)) avgTemp = (t1 + t2) / 2.0;
  else if (!isnan(t1))               avgTemp = t1;
  else if (!isnan(t2))               avgTemp = t2;
  else                               avgTemp = -999;

  if      (!isnan(h1) && !isnan(h2)) avgHumidity = (h1 + h2) / 2.0 + HUMIDITY_OFFSET;
  else if (!isnan(h1))               avgHumidity = h1 + HUMIDITY_OFFSET;
  else if (!isnan(h2))               avgHumidity = h2 + HUMIDITY_OFFSET;
  else                               avgHumidity = -999;
}

// ============================================================
//  Deep Sleep
// ============================================================
void enterDeepSleep() {
  Serial.println("[SYS] Entering deep sleep");

  sleepHeartSensor();

  currentState = PASSIVE;
  updateDHTAverages();
  updateOLED();
  delay(300);

  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  wifiStarted   = false;
  wifiConnected = false;
  timeSynced    = false;

  esp_sleep_enable_timer_wakeup(DEEP_SLEEP_TIME_US);
  esp_sleep_enable_ext0_wakeup(GPIO_NUM_15, 0);

  rtc_gpio_pullup_en(GPIO_NUM_15);
  rtc_gpio_pulldown_dis(GPIO_NUM_15);

  delay(100);
  esp_deep_sleep_start();
}

// ============================================================
//  SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(100);

  // ── LittleFS init ──────────────────────────────────────────
  initFS();

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  // ── OLED init ──────────────────────────────────────────────
  Wire.begin(OLED_SDA, OLED_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("[OLED] Allocation failed"));
    for (;;);
  }
  display.clearDisplay();
  display.display();

  dht1.begin();
  dht2.begin();

  switchState     = (digitalRead(BUTTON_PIN) == LOW);
  lastSwitchState = switchState;

  updateDHTAverages();
  currentState = PASSIVE;
  updateOLED();

  // ── Button ON: HR session ───────────────────────────────────
  if (switchState) {
    Serial.println("[SYS] Button ON — HR session");

    if (initHeartSensor()) {
      currentState = WAITING_FOR_FINGER;
      resetHeartMeasurementVariables();
      updateOLED();
    }

    startWiFi();

  // ── Button OFF: env wake ────────────────────────────────────
  } else {
    Serial.println("[SYS] Button OFF — env wake");

    startWiFi();

    unsigned long offStart = millis();
    while (millis() - offStart < WIFI_TIMEOUT) {
      handleWiFi();
      if (WiFi.status() == WL_CONNECTED) break;
      delay(50);
    }

    // Send current env reading (online or offline)
    if (avgTemp != -999) {
      sendEnvData();
    }

    delay(ENV_SCREEN_HOLD_DURATION);
    enterDeepSleep();
  }
}

// ============================================================
//  MAIN LOOP
// ============================================================
void loop() {
  handleWiFi();   // also triggers flushOfflineLog() on connect

  switchState = (digitalRead(BUTTON_PIN) == LOW);

  // ── Latch change ────────────────────────────────────────────
  if (switchState != lastSwitchState) {
    if (switchState) {
      Serial.println("[SYS] Button ON");

      if (!initHeartSensor()) {
        currentState = PASSIVE;
        updateOLED();
        lastSwitchState = switchState;
        return;
      }

      resetHeartMeasurementVariables();
      currentState = WAITING_FOR_FINGER;
      updateOLED();
      startWiFi();

    } else {
      Serial.println("[SYS] Button OFF");
      enterDeepSleep();
    }

    lastSwitchState = switchState;
  }

  if (!switchState) {
    enterDeepSleep();
  }

  // ── Waiting for finger ──────────────────────────────────────
  if (currentState == WAITING_FOR_FINGER) {
    long irValue = particleSensor.getIR();

    if (irValue >= FINGER_ON_THRESHOLD) {
      measurementStartTime = millis();
      lastSecondTick       = millis();
      resetHeartMeasurementVariables();
      currentState = MEASURING;
      updateOLED();
    } else {
      delay(100);
      return;
    }
  }

  long irValue = particleSensor.getIR();

  // ── Finger removed during measurement ──────────────────────
  if (currentState == MEASURING && irValue < FINGER_OFF_THRESHOLD) {
    resetHeartMeasurementVariables();
    currentState = WAITING_FOR_FINGER;
    updateOLED();
    delay(100);
    return;
  }

  // ── HR Measurement (30 seconds) ─────────────────────────────
  if (currentState == MEASURING) {

    if (millis() - lastSecondTick >= 1000) {
      lastSecondTick = millis();
      timeLeft--;
      if (timeLeft < 0) timeLeft = 0;
      updateOLED();
    }

    if (checkForBeat(irValue) == true) {
      unsigned long now = millis();

      if (lastBeat > 0) {
        long delta = now - lastBeat;

        if (delta >= 333 && delta <= 1500) {
          float currentBPM = 60000.0 / delta;

          if (currentBPM >= MIN_BPM && currentBPM <= MAX_BPM) {
            bpm = currentBPM;

            rates[rateSpot] = (byte)bpm;
            rateSpot++;
            rateSpot %= RATE_SIZE;

            if (validRateCount < RATE_SIZE) validRateCount++;

            beatAvg = getDisplayedBPM();
            updateOLED();
          }
        }
      }

      lastBeat = now;
    }

    if (timeLeft == 0) {
      currentState = CALCULATING_SPO2;
      updateOLED();
    }
  }

  // ── SpO2 Calculation ────────────────────────────────────────
  if (currentState == CALCULATING_SPO2) {
    uint32_t irBuffer[100];
    uint32_t redBuffer[100];

    for (int i = 0; i < 100; i++) {
      while (!particleSensor.available()) particleSensor.check();
      redBuffer[i] = particleSensor.getRed();
      irBuffer[i]  = particleSensor.getIR();
      particleSensor.nextSample();
    }

    maxim_heart_rate_and_oxygen_saturation(
      irBuffer, 100, redBuffer,
      &finalSpO2, &validSPO2,
      &dummyHR,   &dummyValidHR
    );

    currentState       = RESULTS_READY;
    resultDisplayStart = millis();
    updateOLED();

    sendHRData();
  }

  if (currentState == RESULTS_READY) {
    if (millis() - resultDisplayStart >= RESULT_DISPLAY_DURATION) {
      resetHeartMeasurementVariables();
      currentState = WAITING_FOR_FINGER;
      updateOLED();
    } else {
      delay(100);
    }
  }
}
