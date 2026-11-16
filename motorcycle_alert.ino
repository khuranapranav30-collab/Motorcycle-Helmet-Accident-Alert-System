/*
  Motorcycle Accident Alert System
  Board: Arduino Nano 33 IoT

  Libraries:
    - WiFiNINA
    - PubSubClient
    - Arduino_LSM6DS3
    - ArduinoJson

  Wiring:
    FSR divider output -> A0
    LED -> D2 through suitable resistor
    Buzzer driver input -> D4
    Push button -> D3 and GND (INPUT_PULLUP)

  GPS is provided separately by the phone.
  Raspberry Pi sends MESSAGE_SENT after Telegram confirms success.
*/

#include <WiFiNINA.h>
#include <PubSubClient.h>
#include <Arduino_LSM6DS3.h>
#include <ArduinoJson.h>
#include <math.h>

// ---------- WIFI CONFIGURATION ----------

const char* WIFI_SSID = "PRANAVIDEAPAD";
const char* WIFI_PASSWORD = "12345678";

// ---------- MQTT CONFIGURATION ----------

const char* MQTT_HOST = "192.168.137.124";
const uint16_t MQTT_PORT = 1883;
const char* MQTT_CLIENT_ID = "motorcycle_nano_01";

const char* TOPIC_TELEMETRY = "motorcycle/telemetry";
const char* TOPIC_ACCIDENT = "motorcycle/accident";
const char* TOPIC_CANCEL = "motorcycle/cancel";
const char* TOPIC_COMMAND = "motorcycle/command";

// ---------- PINS ----------

const int FSR_PIN = A0;
const int LED_PIN = 2;
const int BUZZER_PIN = 4;
const int BUTTON_PIN = 3;

// ---------- TUNING ----------

const int FSR_WORN_THRESHOLD = 150;

const float ACCELERATION_DELTA_THRESHOLD_G = 1.0f;
const float GYRO_THRESHOLD_DPS = 100.0f;

const unsigned long IMU_CONFIRM_WINDOW_MS = 1500;
const unsigned long TELEMETRY_INTERVAL_MS = 1000;
const unsigned long ACCIDENT_COOLDOWN_MS = 15000;

// ---------- OBJECTS ----------

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

bool localAlarmActive = false;
bool accidentLatched = false;
bool helmetWorn = false;
bool imuReady = false;

unsigned long lastTelemetryMs = 0;
unsigned long lastAccidentMs = 0;
unsigned long highAccelMs = 0;
unsigned long highGyroMs = 0;

float ax = 0, ay = 0, az = 0;
float gx = 0, gy = 0, gz = 0;

float accelMagnitude = 0;
float gyroMagnitude = 0;

// ---------- LOCAL ALARM ----------

void setLocalAlarm(bool enabled) {

  localAlarmActive = enabled;

  digitalWrite(LED_PIN, enabled ? HIGH : LOW);

  if (enabled) {
    tone(BUZZER_PIN, 2200);
  } else {
    noTone(BUZZER_PIN);
  }
}

// ---------- WIFI ----------

void connectWiFi() {

  if (WiFi.status() == WL_CONNECTED) return;

  Serial.print("Connecting to Wi-Fi");

  while (WiFi.begin(WIFI_SSID, WIFI_PASSWORD) != WL_CONNECTED) {
    Serial.print(".");
    delay(5000);
  }

  Serial.println("\nWi-Fi connected.");

  Serial.print("Nano IP: ");
  Serial.println(WiFi.localIP());
}

// ---------- MQTT CALLBACK ----------

void mqttCallback(char* topic, byte* payload, unsigned int length) {

  String message;
  message.reserve(length);

  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }

  if (String(topic) != TOPIC_COMMAND) return;

  // Remote cancellation
  if (message == "CANCEL") {

    accidentLatched = false;

    highAccelMs = 0;
    highGyroMs = 0;

    setLocalAlarm(false);

    Serial.println("Remote cancellation received.");
    Serial.println("LED and buzzer OFF.");
  }

  // Telegram message successfully sent
  else if (message == "MESSAGE_SENT") {

    if (accidentLatched || localAlarmActive) {

      accidentLatched = false;

      highAccelMs = 0;
      highGyroMs = 0;

      setLocalAlarm(false);

      Serial.println("Telegram delivery confirmed by Raspberry Pi.");
      Serial.println("LED and buzzer turned OFF.");
    }
  }
}

// ---------- MQTT CONNECTION ----------

void connectMQTT() {

  while (!mqtt.connected()) {

    String clientId =
      String(MQTT_CLIENT_ID) + "_" +
      String(random(0xffff), HEX);

    Serial.print("Connecting to MQTT...");

    if (mqtt.connect(clientId.c_str())) {

      Serial.println("connected.");

      mqtt.subscribe(TOPIC_COMMAND);

    } else {

      Serial.print("failed, state=");
      Serial.println(mqtt.state());

      delay(3000);
    }
  }
}

// ---------- PUBLISH JSON ----------

void publishJson(
  const char* topic,
  JsonDocument& doc,
  bool retained = false
) {

  char output[512];

  size_t n = serializeJson(doc, output, sizeof(output));

  if (n > 0 && mqtt.connected()) {

    bool success = mqtt.publish(topic, output, retained);

    if (!success) {
      Serial.print("MQTT publish failed: ");
      Serial.println(topic);
    }
  }
}

// ---------- IMU ----------

void readIMU() {

  if (IMU.accelerationAvailable()) {
    IMU.readAcceleration(ax, ay, az);
  }

  if (IMU.gyroscopeAvailable()) {
    IMU.readGyroscope(gx, gy, gz);
  }

  accelMagnitude = sqrt(
    ax * ax + ay * ay + az * az
  );

  float deltaFromRest = fabs(accelMagnitude - 1.0f);

  gyroMagnitude = sqrt(
    gx * gx + gy * gy + gz * gz
  );

  unsigned long now = millis();

  if (deltaFromRest >= ACCELERATION_DELTA_THRESHOLD_G) {
    highAccelMs = now;
  }

  if (gyroMagnitude >= GYRO_THRESHOLD_DPS) {
    highGyroMs = now;
  }
}

// ---------- ACCIDENT DETECTION ----------

bool possibleAccidentDetected() {

  unsigned long now = millis();

  bool accelRecent =
    highAccelMs != 0 &&
    (now - highAccelMs <= IMU_CONFIRM_WINDOW_MS);

  bool gyroRecent =
    highGyroMs != 0 &&
    (now - highGyroMs <= IMU_CONFIRM_WINDOW_MS);

  return accelRecent && gyroRecent;
}

// ---------- TELEMETRY ----------

void publishTelemetry() {

  StaticJsonDocument<384> doc;

  doc["helmetWorn"] = helmetWorn;
  doc["fsrRaw"] = analogRead(FSR_PIN);

  doc["accelX_g"] = ax;
  doc["accelY_g"] = ay;
  doc["accelZ_g"] = az;

  doc["gyroX_dps"] = gx;
  doc["gyroY_dps"] = gy;
  doc["gyroZ_dps"] = gz;

  doc["accelMagnitude_g"] = accelMagnitude;
  doc["gyroMagnitude_dps"] = gyroMagnitude;

  doc["accidentActive"] = accidentLatched;

  publishJson(TOPIC_TELEMETRY, doc);
}

// ---------- ACCIDENT EVENT ----------

void publishAccidentEvent() {

  StaticJsonDocument<384> doc;

  doc["event"] = "POSSIBLE_ACCIDENT";
  doc["helmetWorn"] = helmetWorn;

  doc["accelMagnitude_g"] = accelMagnitude;
  doc["gyroMagnitude_dps"] = gyroMagnitude;

  doc["timestamp"] = millis();

  publishJson(TOPIC_ACCIDENT, doc);

  Serial.println("Accident event published to MQTT.");
}

// ---------- CANCELLATION ----------

void publishCancellation() {

  StaticJsonDocument<128> doc;

  doc["event"] = "CANCEL";
  doc["source"] = "motorcycle_button";

  publishJson(TOPIC_CANCEL, doc);
}

// ---------- SERIAL TEST TRIGGER ----------

void checkSerialTestCommand() {

  while (Serial.available() > 0) {

    char command = Serial.read();

    if (command == 'T' || command == 't') {

      Serial.println("Test command received.");

      if (!accidentLatched) {

        accidentLatched = true;
        lastAccidentMs = millis();

        setLocalAlarm(true);
        publishAccidentEvent();

        Serial.println("TEST ACCIDENT TRIGGERED!");

      } else {

        Serial.println("Accident alert is already active!");
      }
    }
  }
}

// ---------- SETUP ----------

void setup() {

  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(FSR_PIN, INPUT);

  setLocalAlarm(false);

  Serial.begin(115200);

  randomSeed(analogRead(A1));

  Serial.println("Starting Motorcycle Alert System...");

  if (!IMU.begin()) {

    Serial.println("ERROR: onboard LSM6DS3 IMU not detected.");

  } else {

    imuReady = true;
    Serial.println("Onboard IMU ready.");
  }

  connectWiFi();

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(512);

  Serial.println("System setup complete.");
  Serial.println("Type T in Serial Monitor to simulate an accident.");
}

// ---------- MAIN LOOP ----------

void loop() {

  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (!mqtt.connected()) {
    connectMQTT();
  }

  mqtt.loop();

  checkSerialTestCommand();

  readIMU();

  int fsrRaw = analogRead(FSR_PIN);
  helmetWorn = (fsrRaw >= FSR_WORN_THRESHOLD);

  // Physical button cancellation

  static bool previousButton = HIGH;

  bool buttonNow = digitalRead(BUTTON_PIN);

  if (previousButton == HIGH && buttonNow == LOW) {

    if (accidentLatched || localAlarmActive) {

      accidentLatched = false;

      highAccelMs = 0;
      highGyroMs = 0;

      setLocalAlarm(false);

      publishCancellation();

      Serial.println("Accident alert cancelled from motorcycle button.");
      Serial.println("LED and buzzer turned OFF.");
    }

    delay(35);
  }

  previousButton = buttonNow;

  unsigned long now = millis();

  // Actual IMU accident detection

  if (
    imuReady &&
    possibleAccidentDetected() &&
    (!accidentLatched ||
     now - lastAccidentMs > ACCIDENT_COOLDOWN_MS)
  ) {

    accidentLatched = true;
    lastAccidentMs = now;

    setLocalAlarm(true);

    publishAccidentEvent();

    Serial.println("Possible accident published.");
  }

  // Publish sensor readings every second

  if (now - lastTelemetryMs >= TELEMETRY_INTERVAL_MS) {

    lastTelemetryMs = now;

    publishTelemetry();
  }
}