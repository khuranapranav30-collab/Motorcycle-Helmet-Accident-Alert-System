#!/usr/bin/env python3
"""Raspberry Pi motorcycle safety dashboard, MQTT receiver and Telegram notifier."""
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_TELEMETRY = "motorcycle/telemetry"
TOPIC_ACCIDENT = "motorcycle/accident"
TOPIC_CANCEL = "motorcycle/cancel"
TOPIC_COMMAND = "motorcycle/command"
TOPIC_PHONE_GPS = "motorcycle/gps"
COUNTDOWN_SECONDS = 20
PHONE_GPS_MAX_AGE_SECONDS = 60
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

events = queue.Queue()
mqtt_client = None
latest_phone_gps = None
latest_phone_gps_lock = threading.Lock()


def valid_coordinates(latitude, longitude):
    try:
        latitude, longitude = float(latitude), float(longitude)
        return -90 <= latitude <= 90 and -180 <= longitude <= 180
    except (TypeError, ValueError):
        return False


def get_fresh_phone_gps():
    with latest_phone_gps_lock:
        fix = dict(latest_phone_gps) if latest_phone_gps else None
    if not fix or time.time() - fix.get("received_at", 0) > PHONE_GPS_MAX_AGE_SECONDS:
        return None
    if not valid_coordinates(fix.get("latitude"), fix.get("longitude")):
        return None
    return fix


def choose_alert_location(event_data):
    latitude, longitude = event_data.get("latitude"), event_data.get("longitude")
    if bool(event_data.get("gpsFix")) and valid_coordinates(latitude, longitude):
        return float(latitude), float(longitude), True, "GPS"
    phone_fix = get_fresh_phone_gps()
    if phone_fix:
        return float(phone_fix["latitude"]), float(phone_fix["longitude"]), True, "Phone GPS"
    return None, None, False, None


def send_telegram_alert(latitude, longitude, gps_fix, gps_source=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram credentials missing. Check TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in your .env file."
        )
    if gps_fix and valid_coordinates(latitude, longitude):
        latitude, longitude = float(latitude), float(longitude)
        source = f"{gps_source}: " if gps_source else ""
        location_text = f"{source}{latitude:.6f}, {longitude:.6f}"
        maps_url = f"https://www.google.com/maps?q={latitude},{longitude}"
    else:
        location_text = "GPS fix unavailable. Please contact the rider."
        maps_url = None

    message = (
        "🚨 POSSIBLE MOTORCYCLE ACCIDENT 🚨\n\n"
        "The motorcycle safety system detected a possible accident.\n"
        "Please contact the rider and arrange help.\n\n"
        f"{location_text}"
    )
    if maps_url:
        message += f"\n\nOpen location in Google Maps:\n{maps_url}"

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API rejected the message: {result}")
    return result


class AccidentDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Motorcycle Safety Monitoring")
        self.root.geometry("780x650")
        self.root.minsize(650, 560)
        self.pending = False
        self.deadline = 0
        self.current_event = None
        self.countdown_job = None
        self.last_telemetry = {}

        self.status_var = tk.StringVar(value="Connecting to MQTT...")
        self.helmet_var = tk.StringVar(value="Unknown")
        self.accident_var = tk.StringVar(value="Normal")
        self.gps_var = tk.StringVar(value="Waiting for phone GPS...")
        self.accel_var = tk.StringVar(value="—")
        self.gyro_var = tk.StringVar(value="—")
        self.fsr_var = tk.StringVar(value="—")
        self.timer_var = tk.StringVar(value="No active countdown")
        self.telegram_var = tk.StringVar(value="Telegram: not sent")

        self._build_ui()
        self._connect_mqtt()
        self.root.after(100, self._process_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text="Motorcycle Accident Alert System",
            font=("Arial", 19, "bold")
        ).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status_var).pack(
            anchor="w", pady=(4, 14)
        )

        readings = ttk.LabelFrame(frame, text="Live sensor readings", padding=12)
        readings.pack(fill="x", pady=5)
        rows = [
            ("Helmet status", self.helmet_var),
            ("Accident status", self.accident_var),
            ("Phone GPS", self.gps_var),
            ("Accelerometer magnitude", self.accel_var),
            ("Gyroscope magnitude", self.gyro_var),
            ("FSR raw value", self.fsr_var),
        ]
        for label, variable in rows:
            row = ttk.Frame(readings)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=27).pack(side="left")
            ttk.Label(row, textvariable=variable,
                      font=("Arial", 10, "bold")).pack(side="left")

        alert = ttk.LabelFrame(frame, text="Emergency countdown", padding=14)
        alert.pack(fill="x", pady=12)
        ttk.Label(alert, textvariable=self.timer_var,
                  font=("Arial", 18, "bold")).pack(pady=5)
        self.cancel_button = ttk.Button(
            alert, text="CANCEL EMERGENCY ALERT",
            command=self.cancel_alert, state="disabled"
        )
        self.cancel_button.pack(pady=7)
        ttk.Label(alert, textvariable=self.telegram_var,
                  wraplength=680).pack(pady=5)
        ttk.Label(
            frame,
            text=("A possible accident is a sensor-based warning, not a confirmed crash. "
                  "Keep this prototype supervised and verify messages during testing."),
            wraplength=700
        ).pack(anchor="w", pady=8)

    def _connect_mqtt(self):
        global mqtt_client
        try:
            mqtt_client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id="raspberry_pi_motorcycle_dashboard"
            )
            mqtt_client.on_connect = self._on_connect
            mqtt_client.on_message = self._on_message
            mqtt_client.on_disconnect = self._on_disconnect
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            mqtt_client.loop_start()
        except Exception as exc:
            self.status_var.set(f"MQTT connection error: {exc}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe([
                (TOPIC_TELEMETRY, 0),
                (TOPIC_ACCIDENT, 1),
                (TOPIC_CANCEL, 1),
                (TOPIC_PHONE_GPS, 0),
            ])
            events.put(("status", f"Connected to MQTT broker at {MQTT_HOST}:{MQTT_PORT}"))
        else:
            events.put(("status", f"MQTT connection failed: {reason_code}"))

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        events.put(("status", f"MQTT disconnected: {reason_code}"))

    def _on_message(self, client, userdata, msg):
        global latest_phone_gps
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if msg.topic == TOPIC_PHONE_GPS:
            if valid_coordinates(data.get("latitude"), data.get("longitude")):
                fix = {
                    "latitude": float(data["latitude"]),
                    "longitude": float(data["longitude"]),
                    "accuracy": data.get("accuracy"),
                    "timestamp": data.get("timestamp"),
                    "received_at": time.time(),
                }
                with latest_phone_gps_lock:
                    latest_phone_gps = fix
                events.put((TOPIC_PHONE_GPS, fix))
            return
        events.put((msg.topic, data))

    def _process_events(self):
        while True:
            try:
                topic, data = events.get_nowait()
            except queue.Empty:
                break
            if topic == "telegram_result":
                status, detail = data
                if status == "sent":
                    self.telegram_var.set("Telegram: emergency message successfully sent.")

                    # Tell the Arduino to switch OFF its LED and buzzer.
                    # Send this only after Telegram confirms successful delivery.
                    if mqtt_client and mqtt_client.is_connected():
                        try:
                            publish_info = mqtt_client.publish(
                                TOPIC_COMMAND, "MESSAGE_SENT", qos=1
                            )
                            if publish_info.rc == mqtt.MQTT_ERR_SUCCESS:
                                self.status_var.set(
                                    "Telegram sent. MESSAGE_SENT command published to Arduino."
                                )
                            else:
                                self.status_var.set(
                                    f"Telegram sent, but MQTT command publish failed (code {publish_info.rc})."
                                )
                        except Exception as exc:
                            self.status_var.set(
                                f"Telegram sent, but could not publish MESSAGE_SENT: {exc}"
                            )
                    else:
                        self.status_var.set(
                            "Telegram sent, but MQTT is disconnected; Arduino was not notified."
                        )
                else:
                    self.telegram_var.set(f"Telegram sending failed: {detail}")
            elif topic == "status":
                self.status_var.set(data)
            elif topic == TOPIC_TELEMETRY:
                self._update_telemetry(data)
            elif topic == TOPIC_PHONE_GPS:
                self._update_phone_gps(data)
            elif topic == TOPIC_ACCIDENT:
                self._start_countdown(data)
            elif topic == TOPIC_CANCEL:
                self._cancel_from_motorcycle()
        self.root.after(100, self._process_events)

    def _update_telemetry(self, data):
        self.last_telemetry = data
        self.helmet_var.set("Worn" if data.get("helmetWorn") else "Not detected")
        self.accident_var.set(
            "Possible accident" if data.get("accidentActive") else "Normal"
        )
        self.fsr_var.set(str(data.get("fsrRaw", "—")))
        try:
            self.accel_var.set(f'{float(data.get("accelMagnitude_g", 0)):.2f} g')
        except (TypeError, ValueError):
            self.accel_var.set("—")
        try:
            self.gyro_var.set(f'{float(data.get("gyroMagnitude_dps", 0)):.1f} °/s')
        except (TypeError, ValueError):
            self.gyro_var.set("—")
        if get_fresh_phone_gps() is None and not data.get("gpsFix"):
            self.gps_var.set("Waiting for phone GPS...")

    def _update_phone_gps(self, data):
        accuracy = data.get("accuracy")
        coords = f'{data["latitude"]:.6f}, {data["longitude"]:.6f}'
        if accuracy is not None:
            coords += f' (accuracy ±{float(accuracy):.0f} m)'
        self.gps_var.set(coords)

    def _start_countdown(self, data):
        if self.pending:
            return
        self.pending = True
        self.current_event = data
        self.deadline = time.monotonic() + COUNTDOWN_SECONDS
        self.cancel_button.config(state="normal")
        self.accident_var.set("POSSIBLE ACCIDENT – VERIFY")
        self.telegram_var.set("Telegram: waiting for countdown")
        self._tick_countdown()

    def _tick_countdown(self):
        if not self.pending:
            return
        remaining = max(0, int(self.deadline - time.monotonic() + 0.999))
        self.timer_var.set(f"Emergency message in {remaining} seconds")
        if remaining <= 0:
            self.pending = False
            self.cancel_button.config(state="disabled")
            self.timer_var.set("Countdown finished – sending Telegram message")
            self._send_telegram_async(self.current_event or {})
        else:
            self.countdown_job = self.root.after(200, self._tick_countdown)

    def _send_telegram_async(self, event_data):
        latitude, longitude, gps_fix, gps_source = choose_alert_location(event_data)

        def worker():
            try:
                result = send_telegram_alert(latitude, longitude, gps_fix, gps_source)
                events.put(("telegram_result", ("sent", result)))
            except Exception as exc:
                events.put(("telegram_result", ("error", str(exc))))

        self.telegram_var.set("Telegram: sending emergency message...")
        threading.Thread(target=worker, daemon=True).start()

    def cancel_alert(self):
        if not self.pending:
            return
        self.pending = False
        if self.countdown_job:
            self.root.after_cancel(self.countdown_job)
            self.countdown_job = None
        if mqtt_client and mqtt_client.is_connected():
            mqtt_client.publish(TOPIC_COMMAND, "CANCEL", qos=1)
            mqtt_client.publish(
                TOPIC_CANCEL,
                json.dumps({"event": "CANCEL", "source": "raspberry_pi"}),
                qos=1
            )
        self.cancel_button.config(state="disabled")
        self.timer_var.set("Emergency alert cancelled")
        self.telegram_var.set("Telegram: not sent (cancelled)")
        self.accident_var.set("Normal")

    def _cancel_from_motorcycle(self):
        if self.pending:
            self.pending = False
            if self.countdown_job:
                self.root.after_cancel(self.countdown_job)
                self.countdown_job = None
            self.cancel_button.config(state="disabled")
            self.timer_var.set("Cancelled from motorcycle button")
            self.telegram_var.set("Telegram: not sent (cancelled)")
            self.accident_var.set("Normal")

    def _on_close(self):
        if mqtt_client:
            try:
                mqtt_client.loop_stop()
                mqtt_client.disconnect()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    AccidentDashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
