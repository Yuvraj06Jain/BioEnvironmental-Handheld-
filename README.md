# 🌿 Bio Environmental Handheld Monitoring System

> A compact, ESP32-based IoT device that monitors personal health metrics and surrounding environmental conditions — logging data locally, syncing to the cloud, and alerting users to heat stress risk in real time.

---

## Team Members

1) Yuvraj Jain || 2025101140

2) Riya Panchmahalkar || 2025101150

3) Malini Goyal || 2025117007

4) Abhipsa Mishra || 2025101038

---

## 📌 Problem Statement

In environments where heat stress, poor air quality, or physiological strain can go unnoticed — such as outdoor worksites, labs, classrooms, or even daily commutes — there is no affordable, portable tool that simultaneously tracks both **the person** and **their surroundings**. Standard fitness trackers ignore the environment. Weather stations ignore the individual. This project bridges that gap.

The **Bio Environmental Handheld Monitoring System** is a single handheld device that captures heart rate, SpO2, temperature, and humidity together — correlating them into meaningful health risk indicators like **WBGT (Wet Bulb Globe Temperature)** — and makes that data available both on-device and remotely.

---

## 🔧 Hardware

| Component | Role |
|---|---|
| ESP32 DevKit | Main microcontroller |
| MAX30102 | Heart rate & SpO2 sensor (I²C on GPIO 32/33) |
| DHT11 × 2 | Temperature & humidity sensors (GPIO 4 & 5, averaged) |
| SSD1306 OLED (0.96") | Real-time display (I²C on GPIO 21/22) |
| PBS-11A Latching Push Button | User-triggered HR scan (GPIO 15) |
| TP4056 (6-pin Type-C) | LiPo charging module with overcurrent protection |
| 3.7V 400mAh LiPo | Portable power supply |
| Zero PCB | Soldered prototype board |

---

## ✨ Implemented Features

### 🫀 Health Monitoring
- **Heart Rate** — measured via MAX30102 using a rolling 8-sample average (`RATE_SIZE = 8`) over a 30-second scan window for stable readings.
- **SpO2 (Blood Oxygen Saturation)** — computed from a 100-sample buffer collected at the end of each scan session using red/IR ratio analysis.

### 🌡️ Environmental Monitoring
- **Temperature & Humidity** — dual DHT11 sensors read independently and averaged to reduce per-sensor inaccuracy.
- **WBGT (Wet Bulb Globe Temperature)** — computed on-device from the averaged temperature and humidity readings (see section below).

### 🖥️ Real-Time OLED Display
- Displays live readings: heart rate (BPM), SpO2 (%), temperature (°C), humidity (%), and WBGT index.
- Screen state is preserved across deep sleep cycles using `RTC_DATA_ATTR` — the display is initialized only once and GDDRAM content is retained via continuous 3.3V supply.

### 💤 Deep Sleep Power Management
- The device enters **deep sleep** between readings to conserve battery.
- **Timer wake (every 120 seconds):** automatically wakes for a DHT environmental reading cycle.
- **EXT0 wake (GPIO 15 LOW → HIGH):** the latching button triggers a full HR + SpO2 + environmental scan session on demand.

### ☁️ Cloud / Network Connectivity
- On wake, the ESP32 connects to Wi-Fi and **HTTP POSTs** a JSON payload to a local **FastAPI** server.
- The backend stores readings in a **MongoDB** database via the **Motor** async driver.
- A styled **HTML dashboard** displays all readings with color-coded **normal / abnormal badges** for quick interpretation.

### 📦 Offline Logging & Store-and-Forward
- If Wi-Fi is unavailable, readings are written to **LittleFS** (flash filesystem) as a local log.
- On the next successful connection, all queued offline entries are **automatically forwarded** to the backend before resuming normal operation — no data is lost.

---

## 🌡️ What is WBGT and How is it Implemented Here?

### What is WBGT?
**Wet Bulb Globe Temperature (WBGT)** is an internationally recognised heat stress index used in occupational health, sports medicine, and military environments. Unlike plain air temperature, WBGT accounts for the **combined effect of heat, humidity, and (in full outdoor variants) solar radiation and wind** on the human body. It is a better predictor of heat-related illness risk than temperature alone.

WBGT is used to determine safe activity thresholds:

| WBGT (°C) | Risk Level | Recommended Action |
|---|---|---|
| < 18 | Low | No restriction |
| 18 – 23 | Moderate | Light caution |
| 23 – 28 | High | Limit strenuous activity |
| 28 – 32 | Very High | Mandatory rest periods |
| > 32 | Extreme | Stop outdoor exertion |

### How It Is Implemented
Since this is an **indoor/shaded-environment device** (no solar radiation sensor), the simplified indoor WBGT formula is used, which requires only **dry-bulb temperature (T)** and **relative humidity (RH)**:

**Step 1 — Estimate Wet Bulb Temperature (Tw) using the Stull approximation:**
```
Tw = T × atan(0.151977 × √(RH + 8.313659))
   + atan(T + RH)
   - atan(RH - 1.676331)
   + 0.00391838 × RH^1.5 × atan(0.023101 × RH)
   - 4.686035
```

**Step 2 — Compute Indoor WBGT:**
```
WBGT = 0.7 × Tw + 0.3 × T
```
*(The 0.7 weighting on wet bulb reflects that humidity is the dominant physiological stressor; the 0.3 dry bulb component represents ambient heat load.)*

Both T and RH come from the **averaged dual-DHT11 readings**, giving the WBGT estimate a more reliable input than a single sensor would provide. The result is displayed live on the OLED and included in every data packet sent to the backend, where the dashboard flags values above threshold with an **abnormal badge**.

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────┐
│              ESP32 DevKit                    │
│                                              │
│  DHT11 × 2 ──► Temp/Humidity Average        │
│  MAX30102  ──► HR + SpO2 (on button press)  │
│                      │                       │
│              WBGT Calculation                │
│                      │                       │
│         ┌────────────┴────────────┐          │
│         ▼                         ▼          │
│    OLED Display             LittleFS Log     │
│    (live readings)          (if offline)     │
│                                              │
│         └────────────┬────────────┘          │
│                      ▼                       │
│               Wi-Fi HTTP POST                │
└──────────────────────┼──────────────────────┘
                       ▼
          ┌────────────────────────┐
          │   FastAPI Backend      │
          │   + Motor + MongoDB    │
          │   + HTML Dashboard     │
          └────────────────────────┘
```

---

## 📡 Backend

- **Framework:** FastAPI (Python)
- **Database:** MongoDB via Motor (async driver)
- **Dashboard:** Styled HTML with color-coded normal/abnormal status badges for all metrics
- **Planned Cloud Migration:** FastAPI on Render + MongoDB Atlas for remote accessibility beyond the local network

---


## 📄 License

This project was developed for academic purposes as part of an IoT course assignment.