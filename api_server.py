#!/usr/bin/env python3
"""
Serves sensor data, incidents, and system status via HTTP.
Used by the dashboard and n8n workflows.
"""

import sqlite3
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from pathlib import Path

app = Flask(__name__, static_folder="/home/admin/sentra/dashboard",
            static_url_path="/dashboard")
CORS(app)

DB_PATH = "/home/admin/sentra/data/sentra.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Dashboard

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# API: Current Status

@app.route("/api/status")
def api_status():
    """Get current system status - single call for dashboard header."""
    conn = get_db()
    c = conn.cursor()

    # Latest reading
    c.execute("SELECT * FROM sensor_readings ORDER BY timestamp DESC LIMIT 1")
    latest = c.fetchone()

    # System status
    c.execute("SELECT key, value FROM system_status")
    status = {row["key"]: row["value"] for row in c.fetchall()}

    # Counts
    c.execute("SELECT COUNT(*) as cnt FROM incidents WHERE acknowledged = 0")
    unacked = c.fetchone()["cnt"]

    c.execute(
        "SELECT COUNT(*) as cnt FROM power_events WHERE event_type = 'OUTAGE_START'")
    outages = c.fetchone()["cnt"]

    c.execute("SELECT COUNT(*) as cnt FROM sensor_readings")
    total_readings = c.fetchone()["cnt"]

    # Uptime (first reading timestamp)
    c.execute("SELECT timestamp FROM sensor_readings ORDER BY timestamp ASC LIMIT 1")
    first = c.fetchone()
    uptime_since = first["timestamp"] if first else None

    conn.close()

    return jsonify({
        "current": {
            "temperature_c": latest["temperature_c"] if latest else None,
            "battery_pct": latest["battery_pct"] if latest else None,
            "charging": latest["battery_charging"] if latest else None,
            "power_input": latest["power_input"] if latest else None,
            "timestamp": latest["timestamp"] if latest else None,
        },
        "stats": {
            "total_readings": total_readings,
            "unacknowledged_incidents": unacked,
            "total_outages": outages,
            "uptime_since": uptime_since,
        },
        "system": status,
    })


# API: Temperature History

@app.route("/api/temperature")
def api_temperature():
    """Get temperature history. ?hours=24 (default) or ?hours=168 for 7 days."""
    hours = int(request.args.get("hours", 24))
    limit = int(request.args.get("limit", 1440))

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, temperature_c FROM sensor_readings
        WHERE timestamp > ? AND temperature_c IS NOT NULL
        ORDER BY timestamp ASC
        LIMIT ?
    """, (since, limit))
    rows = [{"timestamp": r["timestamp"], "temperature_c": r["temperature_c"]}
            for r in c.fetchall()]

    # Stats
    if rows:
        temps = [r["temperature_c"] for r in rows]
        stats = {
            "min": round(min(temps), 2),
            "max": round(max(temps), 2),
            "avg": round(sum(temps) / len(temps), 2),
            "count": len(temps),
        }
    else:
        stats = {"min": None, "max": None, "avg": None, "count": 0}

    conn.close()
    return jsonify({"readings": rows, "stats": stats, "hours": hours})


# API: Battery History

@app.route("/api/battery")
def api_battery():
    hours = int(request.args.get("hours", 24))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, battery_pct, battery_charging, power_input
        FROM sensor_readings
        WHERE timestamp > ? AND battery_pct IS NOT NULL
        ORDER BY timestamp ASC
    """, (since,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({"readings": rows, "hours": hours})


# API: Incidents

@app.route("/api/incidents")
def api_incidents():
    limit = int(request.args.get("limit", 50))
    severity = request.args.get("severity")
    unacked_only = request.args.get("unacked", "false").lower() == "true"

    conn = get_db()
    c = conn.cursor()

    query = "SELECT * FROM incidents WHERE 1=1"
    params = []

    if severity:
        query += " AND severity = ?"
        params.append(severity.upper())

    if unacked_only:
        query += " AND acknowledged = 0"

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({"incidents": rows, "count": len(rows)})


@app.route("/api/incidents/<int:incident_id>/acknowledge", methods=["POST"])
def acknowledge_incident(incident_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE incidents SET acknowledged = 1 WHERE id = ?", (incident_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": incident_id})


# API: Power Events

@app.route("/api/power-events")
def api_power_events():
    limit = int(request.args.get("limit", 50))
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM power_events ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({"events": rows, "count": len(rows)})


# API: Daily Summary (for n8n reports)

@app.route("/api/summary")
def api_summary():
    """Generate a summary for the past N days. Used by n8n for reports."""
    days = int(request.args.get("days", 1))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    conn = get_db()
    c = conn.cursor()

    # Temperature stats
    c.execute("""
        SELECT
            MIN(temperature_c) as temp_min,
            MAX(temperature_c) as temp_max,
            AVG(temperature_c) as temp_avg,
            COUNT(*) as reading_count
        FROM sensor_readings
        WHERE timestamp > ? AND temperature_c IS NOT NULL
    """, (since,))
    temp_stats = dict(c.fetchone())

    # Time outside safe range
    c.execute("""
        SELECT COUNT(*) as cnt FROM sensor_readings
        WHERE timestamp > ?
        AND temperature_c IS NOT NULL
        AND (temperature_c > 8.0 OR temperature_c < 2.0)
    """, (since,))
    unsafe_readings = c.fetchone()["cnt"]

    # Incidents
    c.execute("""
        SELECT incident_type, severity, COUNT(*) as cnt
        FROM incidents WHERE timestamp > ?
        GROUP BY incident_type, severity
    """, (since,))
    incident_summary = [dict(r) for r in c.fetchall()]

    # Power events
    c.execute("""
        SELECT COUNT(*) as cnt FROM power_events
        WHERE timestamp > ? AND event_type = 'OUTAGE_START'
    """, (since,))
    outage_count = c.fetchone()["cnt"]

    conn.close()

    total = temp_stats["reading_count"] or 1
    compliance_pct = round(((total - unsafe_readings) / total) * 100, 1)

    return jsonify({
        "period_days": days,
        "temperature": {
            "min_c": round(temp_stats["temp_min"], 2) if temp_stats["temp_min"] else None,
            "max_c": round(temp_stats["temp_max"], 2) if temp_stats["temp_max"] else None,
            "avg_c": round(temp_stats["temp_avg"], 2) if temp_stats["temp_avg"] else None,
            "readings_total": total,
            "readings_unsafe": unsafe_readings,
            "compliance_pct": compliance_pct,
        },
        "incidents": incident_summary,
        "power_outages": outage_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


# API: Health Check (for n8n to ping)

@app.route("/api/health")
def api_health():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sensor_readings")
        count = c.fetchone()[0]
        conn.close()
        return jsonify({"status": "healthy", "readings": count})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    print("=" * 60)
    print("Sentra AI - API Server")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
