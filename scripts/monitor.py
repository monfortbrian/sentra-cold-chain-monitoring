#!/usr/bin/env python3
"""
Reads DS18B20 temperature sensor + PiJuice battery status
Logs everything to SQLite with anomaly detection
"""

import os
import sys
import time
import json
import glob
import sqlite3
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path


# Configuration

CONFIG = {
    "db_path": "/home/admin/sentra/data/sentra.db",
    "log_path": "/home/admin/sentra/logs/monitor.log",
    "read_interval_seconds": 60,          # read sensors every 60s
    "temp_alert_high_c": 8.0,             # vaccine fridge max safe temp
    "temp_alert_low_c": 2.0,              # vaccine fridge min safe temp
    "battery_alert_low_pct": 20,          # battery low threshold
    "battery_critical_pct": 10,           # trigger safe shutdown
    "alert_cooldown_seconds": 300,        # don't repeat same alert within 5 min
    "max_sensor_retries": 3,
}


# Logging

def setup_logging():
    log_dir = Path(CONFIG["log_path"]).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(CONFIG["log_path"]),
            logging.StreamHandler(sys.stdout),
        ],
    )


# Database

def init_db():
    db_dir = Path(CONFIG["db_path"]).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CONFIG["db_path"])
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            temperature_c REAL,
            battery_pct INTEGER,
            battery_charging TEXT,
            power_input TEXT,
            power_input_stable INTEGER DEFAULT 1,
            sensor_id TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            incident_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            value REAL,
            acknowledged INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS power_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            battery_pct INTEGER,
            duration_seconds INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS system_status (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)

    # Create indexes for fast queries
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_ts ON sensor_readings(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_incidents_ts ON incidents(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_power_ts ON power_events(timestamp)")

    conn.commit()
    return conn


# 1-Wire Temperature Sensor (DS18B20)

def find_sensor_ids():
    """Discover all connected DS18B20 sensors."""
    base_dir = "/sys/bus/w1/devices/"
    sensors = glob.glob(base_dir + "28-*")
    return [os.path.basename(s) for s in sensors]


def read_temperature(sensor_id, retries=None):
    """Read temperature from a specific DS18B20 sensor."""
    if retries is None:
        retries = CONFIG["max_sensor_retries"]

    device_file = f"/sys/bus/w1/devices/{sensor_id}/w1_slave"

    for attempt in range(retries):
        try:
            with open(device_file, "r") as f:
                lines = f.readlines()

            # First line ends with YES if CRC check passed
            if lines[0].strip().endswith("YES"):
                # Second line contains t=XXXXX (temp in millidegrees)
                equals_pos = lines[1].find("t=")
                if equals_pos != -1:
                    temp_string = lines[1][equals_pos + 2:]
                    temp_c = float(temp_string) / 1000.0

                    # Sanity check: reject obviously wrong readings
                    if -55.0 <= temp_c <= 125.0:
                        return round(temp_c, 2)
                    else:
                        logging.warning(
                            f"Sensor {sensor_id} returned out-of-range: {temp_c}°C")
            else:
                logging.warning(
                    f"Sensor {sensor_id} CRC check failed (attempt {attempt + 1})")

        except FileNotFoundError:
            logging.error(f"Sensor {sensor_id} not found at {device_file}")
            return None
        except Exception as e:
            logging.error(f"Error reading sensor {sensor_id}: {e}")

        if attempt < retries - 1:
            time.sleep(1)

    logging.error(
        f"Failed to read sensor {sensor_id} after {retries} attempts")
    return None


# PiJuice Battery Monitor

class BatteryMonitor:
    def __init__(self):
        self.pijuice = None
        self._init_pijuice()

    def _init_pijuice(self):
        try:
            from pijuice import PiJuice
            self.pijuice = PiJuice(1, 0x14)
            logging.info("PiJuice initialized successfully")
        except ImportError:
            logging.warning(
                "PiJuice library not installed - battery monitoring disabled")
        except Exception as e:
            logging.warning(
                f"PiJuice init failed: {e} - battery monitoring disabled")

    def get_status(self):
        """Returns dict with battery_pct, charging, power_input."""
        result = {
            "battery_pct": None,
            "charging": "UNKNOWN",
            "power_input": "UNKNOWN",
        }

        if self.pijuice is None:
            return result

        try:
            charge = self.pijuice.status.GetChargeLevel()
            if charge.get("error") == "NO_ERROR":
                result["battery_pct"] = charge["data"]

            status = self.pijuice.status.GetStatus()
            if status.get("error") == "NO_ERROR":
                data = status["data"]
                result["charging"] = data.get("battery", "UNKNOWN")
                result["power_input"] = data.get("powerInput", "UNKNOWN")

        except Exception as e:
            logging.error(f"PiJuice read error: {e}")

        return result


# Anomaly Detection

class AnomalyDetector:
    def __init__(self):
        self.last_alerts = {}  # type -> timestamp

    def _can_alert(self, alert_type):
        """Prevent alert spam with cooldown."""
        now = time.time()
        last = self.last_alerts.get(alert_type, 0)
        if now - last < CONFIG["alert_cooldown_seconds"]:
            return False
        self.last_alerts[alert_type] = now
        return True

    def check_temperature(self, temp_c, conn):
        """Check temperature against thresholds."""
        incidents = []

        if temp_c is None:
            if self._can_alert("sensor_failure"):
                incidents.append({
                    "type": "SENSOR_FAILURE",
                    "severity": "HIGH",
                    "message": "Temperature sensor not responding",
                    "value": 0,
                })
            return incidents

        if temp_c > CONFIG["temp_alert_high_c"]:
            if self._can_alert("temp_high"):
                incidents.append({
                    "type": "TEMP_HIGH",
                    "severity": "CRITICAL",
                    "message": f"Temperature {temp_c}°C exceeds safe limit ({CONFIG['temp_alert_high_c']}°C). Vaccine integrity at risk!",
                    "value": temp_c,
                })

        elif temp_c < CONFIG["temp_alert_low_c"]:
            if self._can_alert("temp_low"):
                incidents.append({
                    "type": "TEMP_LOW",
                    "severity": "WARNING",
                    "message": f"Temperature {temp_c}°C below minimum ({CONFIG['temp_alert_low_c']}°C). Possible freezing risk.",
                    "value": temp_c,
                })

        # Trend detection: check if temp is rising fast
        try:
            c = conn.cursor()
            c.execute("""
                SELECT temperature_c FROM sensor_readings
                WHERE temperature_c IS NOT NULL
                ORDER BY timestamp DESC LIMIT 5
            """)
            recent = [row[0] for row in c.fetchall()]
            if len(recent) >= 5:
                avg_old = sum(recent[2:]) / len(recent[2:])
                avg_new = sum(recent[:2]) / len(recent[:2])
                rate = avg_new - avg_old
                if rate > 2.0:  # rising more than 2°C in 5 readings
                    if self._can_alert("temp_rising"):
                        incidents.append({
                            "type": "TEMP_RISING_FAST",
                            "severity": "WARNING",
                            "message": f"Temperature rising rapidly ({rate:+.1f}°C trend). Possible door open or cooling failure.",
                            "value": rate,
                        })
        except Exception as e:
            logging.debug(f"Trend check error: {e}")

        return incidents

    def check_battery(self, battery_status, conn):
        """Check battery and power status."""
        incidents = []
        pct = battery_status["battery_pct"]
        power = battery_status["power_input"]

        if pct is not None:
            if pct <= CONFIG["battery_critical_pct"]:
                if self._can_alert("battery_critical"):
                    incidents.append({
                        "type": "BATTERY_CRITICAL",
                        "severity": "CRITICAL",
                        "message": f"Battery at {pct}%! System will shut down soon to protect SD card.",
                        "value": pct,
                    })
            elif pct <= CONFIG["battery_alert_low_pct"]:
                if self._can_alert("battery_low"):
                    incidents.append({
                        "type": "BATTERY_LOW",
                        "severity": "WARNING",
                        "message": f"Battery at {pct}%. Power may be interrupted.",
                        "value": pct,
                    })

        # Detect power loss (running on battery)
        if power in ("NOT_PRESENT", "BAD"):
            if self._can_alert("power_loss"):
                incidents.append({
                    "type": "POWER_OUTAGE",
                    "severity": "HIGH",
                    "message": f"Main power lost! Running on battery ({pct}%).",
                    "value": pct or 0,
                })
                # Log power event
                try:
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO power_events (timestamp, event_type, battery_pct)
                        VALUES (?, 'OUTAGE_START', ?)
                    """, (datetime.now(timezone.utc).isoformat(), pct))
                    conn.commit()
                except Exception as e:
                    logging.error(f"Power event log error: {e}")

        return incidents


# Alert Queue (for offline sync)

def save_incidents(conn, incidents):
    """Save incidents to database."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for inc in incidents:
        c.execute("""
            INSERT INTO incidents (timestamp, incident_type, severity, message, value)
            VALUES (?, ?, ?, ?, ?)
        """, (now, inc["type"], inc["severity"], inc["message"], inc["value"]))
        logging.warning(
            f"INCIDENT [{inc['severity']}] {inc['type']}: {inc['message']}")
    conn.commit()


# System Status

def update_system_status(conn, key, value):
    now = datetime.now(timezone.utc).isoformat()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO system_status (key, value, updated_at)
        VALUES (?, ?, ?)
    """, (key, str(value), now))
    conn.commit()


# Main Loop

class SentraMonitor:
    def __init__(self):
        self.running = True
        self.conn = None
        self.battery = BatteryMonitor()
        self.detector = AnomalyDetector()
        self.sensors = []
        self.reading_count = 0

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, signum, frame):
        logging.info(f"Shutdown signal received ({signum}). Cleaning up...")
        self.running = False

    def start(self):
        setup_logging()
        logging.info("=" * 60)
        logging.info("Sentra AI Monitor - Starting up")
        logging.info("=" * 60)

        self.conn = init_db()

        # Discover sensors
        self.sensors = find_sensor_ids()
        if self.sensors:
            logging.info(
                f"Found {len(self.sensors)} temperature sensor(s): {self.sensors}")
        else:
            logging.warning(
                "No DS18B20 sensors found! Temperature monitoring disabled.")
            logging.warning(
                "Check: 1-Wire enabled? Sensor wired correctly? 4.7kΩ resistor?")

        update_system_status(self.conn, "monitor_started",
                             datetime.now(timezone.utc).isoformat())
        update_system_status(self.conn, "sensor_count", len(self.sensors))

        logging.info(f"Reading interval: {CONFIG['read_interval_seconds']}s")
        logging.info(
            f"Temp safe range: {CONFIG['temp_alert_low_c']}–{CONFIG['temp_alert_high_c']}°C")
        logging.info("Monitor running. Press Ctrl+C to stop.")
        logging.info("-" * 60)

        self._run_loop()

    def _run_loop(self):
        while self.running:
            try:
                self._read_cycle()
            except Exception as e:
                logging.error(f"Read cycle error: {e}")

            # Sleep in small chunks so we can respond to signals
            for _ in range(CONFIG["read_interval_seconds"]):
                if not self.running:
                    break
                time.sleep(1)

        logging.info("Monitor stopped cleanly.")
        if self.conn:
            self.conn.close()

    def _read_cycle(self):
        now = datetime.now(timezone.utc).isoformat()
        self.reading_count += 1

        # Read temperature from first sensor (primary)
        temp_c = None
        sensor_id = None
        if self.sensors:
            sensor_id = self.sensors[0]
            temp_c = read_temperature(sensor_id)

        # Read battery
        bat = self.battery.get_status()

        # Log to database
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO sensor_readings
            (timestamp, temperature_c, battery_pct, battery_charging, power_input, sensor_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now, temp_c, bat["battery_pct"], bat["charging"], bat["power_input"], sensor_id))
        self.conn.commit()

        # Check for anomalies
        incidents = []
        incidents.extend(self.detector.check_temperature(temp_c, self.conn))
        incidents.extend(self.detector.check_battery(bat, self.conn))

        if incidents:
            save_incidents(self.conn, incidents)

        # Update system status
        update_system_status(self.conn, "last_reading", now)
        update_system_status(self.conn, "last_temp_c", temp_c)
        update_system_status(self.conn, "last_battery_pct", bat["battery_pct"])
        update_system_status(self.conn, "total_readings", self.reading_count)

        # Console output
        temp_str = f"{temp_c}°C" if temp_c is not None else "N/A"
        bat_str = f"{bat['battery_pct']}%" if bat["battery_pct"] is not None else "N/A"
        power_str = bat["power_input"]
        alert_str = f" | {len(incidents)} ALERT(S)!" if incidents else ""

        logging.info(
            f"#{self.reading_count} | Temp: {temp_str} | Bat: {bat_str} | Power: {power_str}{alert_str}")


if __name__ == "__main__":
    monitor = SentraMonitor()
    monitor.start()
