from flask import Flask, render_template, jsonify, request
from datetime import datetime, timedelta
import requests
import json
import logging
import boto3
import os

application = Flask(__name__)
app = application

# Configuration
API_URL = "https://pndjbnrup9.execute-api.us-east-1.amazonaws.com/default/lambda-database-x24275387"
REFRESH_INTERVAL = 5  # seconds

# SNS Configuration
SNS_TOPIC_ARN = 'arn:aws:sns:us-east-1:842675896420:sns-x24275387'
SNS_REGION = 'us-east-1'

# Initialize SNS client
try:
    sns_client = boto3.client('sns', region_name=SNS_REGION)
    print("SNS client initialized successfully")
except Exception as e:
    print(f"Failed to initialize SNS client: {e}")
    sns_client = None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# EV Battery Critical Thresholds (based on sensor data)
CRITICAL_THRESHOLDS = {
    "voltage": {"min": 300, "max": 410, "warning_min": 320, "warning_max": 400},
    "current": {"max": 120, "warning_max": 100},
    "temperature": {"max": 65, "warning_max": 55},
    "soc": {"min": 15, "max": 100, "warning_min": 25},
    "soh": {"min": 75, "warning_min": 85}
}

# Email tracking
last_email_sent_time = None
EMAIL_COOLDOWN_SECONDS = 300

def is_sensor_critical(sensor_name, value):
    """Check if a single sensor is at critical level"""
    if value is None:
        return False
    
    thresholds = CRITICAL_THRESHOLDS.get(sensor_name, {})
    
    if sensor_name == "current":
        return value > thresholds.get('max', 120)
    elif sensor_name == "temperature":
        return value > thresholds.get('max', 65)
    elif sensor_name == "soc":
        return value < thresholds.get('min', 15)
    elif sensor_name == "soh":
        return value < thresholds.get('min', 75)
    else:
        min_val = thresholds.get('min')
        max_val = thresholds.get('max')
        if min_val is not None and value < min_val:
            return True
        if max_val is not None and value > max_val:
            return True
    return False

def is_sensor_warning(sensor_name, value):
    """Check if a single sensor is at warning level"""
    if value is None:
        return False
    
    thresholds = CRITICAL_THRESHOLDS.get(sensor_name, {})
    
    if sensor_name == "current":
        warning_max = thresholds.get('warning_max', 100)
        critical_max = thresholds.get('max', 120)
        return warning_max < value <= critical_max
    elif sensor_name == "temperature":
        warning_max = thresholds.get('warning_max', 55)
        critical_max = thresholds.get('max', 65)
        return warning_max < value <= critical_max
    elif sensor_name == "soc":
        warning_min = thresholds.get('warning_min', 25)
        critical_min = thresholds.get('min', 15)
        return critical_min < value <= warning_min
    elif sensor_name == "soh":
        warning_min = thresholds.get('warning_min', 85)
        critical_min = thresholds.get('min', 75)
        return critical_min < value <= warning_min
    else:
        warning_min = thresholds.get('warning_min')
        warning_max = thresholds.get('warning_max')
        critical_min = thresholds.get('min')
        critical_max = thresholds.get('max')
        
        if warning_min is not None and critical_min is not None:
            if critical_min < value <= warning_min:
                return True
        if warning_max is not None and critical_max is not None:
            if warning_max <= value < critical_max:
                return True
    return False

def determine_system_status(data):
    """Determine overall system status based on sensor data"""
    if not data or len(data) == 0:
        return "unknown", "No data available", [], None
    
    latest = data[0]
    
    critical_sensors = []
    warning_sensors = []
    
    sensors_to_check = ['voltage', 'current', 'temperature', 'soc', 'soh']
    
    for sensor in sensors_to_check:
        value = latest.get(sensor)
        if value is not None:
            if is_sensor_critical(sensor, value):
                critical_sensors.append(sensor)
            elif is_sensor_warning(sensor, value):
                warning_sensors.append(sensor)
    
    all_sensors_present = all(latest.get(s) is not None for s in sensors_to_check)
    
    if all_sensors_present and len(critical_sensors) == len(sensors_to_check):
        status = "critical"
        status_message = "CRITICAL - All battery parameters at critical levels"
        issues = [f"{s}: {latest.get(s)}" for s in critical_sensors]
    elif len(critical_sensors) > 0:
        status = "warning"
        status_message = f"WARNING - {len(critical_sensors)} parameter(s) at critical level"
        issues = [f"{s}: {latest.get(s)}" for s in critical_sensors]
        issues.extend([f"{s}: {latest.get(s)}" for s in warning_sensors])
    elif len(warning_sensors) > 0:
        status = "warning"
        status_message = f"WARNING - {len(warning_sensors)} parameter(s) approaching critical levels"
        issues = [f"{s}: {latest.get(s)}" for s in warning_sensors]
    else:
        status = "normal"
        status_message = "NORMAL - All battery parameters within safe range"
        issues = []
    
    return status, status_message, issues, latest

def send_critical_alert_email(data_summary):
    """Send SNS email alert when system is critical"""
    global last_email_sent_time
    
    current_time = datetime.now()
    if last_email_sent_time:
        time_diff = (current_time - last_email_sent_time).total_seconds()
        if time_diff < EMAIL_COOLDOWN_SECONDS:
            logger.info(f"Email cooldown active, skipping")
            return False
    
    if not sns_client:
        logger.error("SNS client not available")
        return False
    
    try:
        subject = f"[CRITICAL] EV Battery Alert - {current_time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        message = f"""
CRITICAL STATUS DETECTED - EV BATTERY MONITORING SYSTEM

Timestamp: {current_time.strftime('%Y-%m-%d %H:%M:%S')}

Current Battery Readings:
--------------------------------------------------
Voltage: {data_summary.get('voltage', 'N/A')} V (Critical: <300V or >410V)
Current: {data_summary.get('current', 'N/A')} A (Critical: >120A)
Temperature: {data_summary.get('temperature', 'N/A')} C (Critical: >65C)
State of Charge: {data_summary.get('soc', 'N/A')}% (Critical: <15%)
State of Health: {data_summary.get('soh', 'N/A')}% (Critical: <75%)
Vehicle ID: {data_summary.get('vehicle_id', 'N/A')}
Record Count: {data_summary.get('total_records', 0)}
--------------------------------------------------

Immediate action required. Check dashboard for details.

Dashboard URL: http://localhost:5001
        """
        
        response = sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
        
        last_email_sent_time = current_time
        logger.info(f"Critical alert email sent")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send SNS email: {e}")
        return False

def fetch_data():
    """Fetch data from Lambda via API Gateway"""
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if isinstance(data, dict):
            if 'data' in data:
                data = data['data']
            elif 'items' in data:
                data = data['items']
            elif 'body' in data:
                try:
                    data = json.loads(data['body'])
                except:
                    data = data['body']
        
        if not isinstance(data, list):
            data = [data] if data else []
        
        data.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        logger.info(f"Fetched {len(data)} records")
        return data, None
        
    except Exception as e:
        error_msg = f"API error: {str(e)}"
        logger.error(error_msg)
        return [], error_msg

@app.route("/")
def index():
    return render_template("dashboard.html", refresh_interval=REFRESH_INTERVAL)

@app.route("/api/data")
def get_data():
    """Get filtered data with status"""
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    vehicle_id = request.args.get('vehicle_id')
    limit = request.args.get('limit', 100, type=int)
    
    data, error = fetch_data()
    
    if error:
        return jsonify({"success": False, "error": error, "data": []}), 200
    
    if not data:
        return jsonify({"success": True, "data": [], "total_records": 0})
    
    filtered = data
    
    if start_date:
        try:
            filtered = [d for d in filtered if d.get('timestamp', '') >= start_date]
        except Exception as e:
            logger.warning(f"Start date filter error: {e}")
    
    if end_date:
        try:
            filtered = [d for d in filtered if d.get('timestamp', '') <= end_date]
        except Exception as e:
            logger.warning(f"End date filter error: {e}")
    
    if vehicle_id:
        filtered = [d for d in filtered if d.get('vehicle_id') == vehicle_id]
    
    if limit and len(filtered) > limit:
        filtered = filtered[:limit]
    
    for record in filtered:
        status, status_message, issues, _ = determine_system_status([record])
        record['status'] = status
        record['status_message'] = status_message
    
    overall_status, overall_message, overall_issues, latest_data = determine_system_status(data)
    
    if overall_status == "critical" and latest_data:
        data_summary = {
            'voltage': latest_data.get('voltage'),
            'current': latest_data.get('current'),
            'temperature': latest_data.get('temperature'),
            'soc': latest_data.get('soc'),
            'soh': latest_data.get('soh'),
            'vehicle_id': latest_data.get('vehicle_id', 'Unknown'),
            'total_records': len(data)
        }
        send_critical_alert_email(data_summary)
    
    return jsonify({
        "success": True,
        "data": filtered,
        "total_records": len(filtered),
        "overall_status": overall_status,
        "overall_status_message": overall_message,
        "timestamp": datetime.now().isoformat()
    })

@app.route("/api/stats")
def get_stats():
    """Get statistical summary"""
    data, error = fetch_data()
    
    if error or not data:
        return jsonify({"success": False, "stats": {}})
    
    def get_stats_list(key):
        values = [d.get(key, 0) for d in data if d.get(key) is not None]
        return values if values else [0]
    
    stats = {
        "voltage": {"current": get_stats_list('voltage')[0], "avg": round(sum(get_stats_list('voltage')) / len(get_stats_list('voltage')), 2)},
        "current": {"current": get_stats_list('current')[0], "avg": round(sum(get_stats_list('current')) / len(get_stats_list('current')), 2)},
        "temperature": {"current": get_stats_list('temperature')[0], "avg": round(sum(get_stats_list('temperature')) / len(get_stats_list('temperature')), 2)},
        "soc": {"current": get_stats_list('soc')[0], "avg": round(sum(get_stats_list('soc')) / len(get_stats_list('soc')), 2)},
        "soh": {"current": get_stats_list('soh')[0], "avg": round(sum(get_stats_list('soh')) / len(get_stats_list('soh')), 2)},
        "total_records": len(data)
    }
    
    overall_status, overall_message, _, _ = determine_system_status(data)
    stats["overall_status"] = overall_status
    stats["overall_status_message"] = overall_message
    
    return jsonify({"success": True, "stats": stats})

@app.route("/api/vehicles")
def get_vehicles():
    """Get list of unique vehicles"""
    data, error = fetch_data()
    
    if error or not data:
        return jsonify({"vehicles": []})
    
    vehicles = list(set([d.get('vehicle_id', 'Unknown') for d in data if d.get('vehicle_id')]))
    return jsonify({"vehicles": vehicles})

@app.route("/api/status")
def get_status():
    """Get API connection status"""
    data, error = fetch_data()
    
    return jsonify({
        "success": error is None,
        "error": error,
        "data_available": len(data) > 0 if data else False,
        "record_count": len(data) if data else 0
    })

if __name__ == "__main__":
    print("=" * 60)
    print("Smart EV Battery Monitoring Dashboard")
    print("=" * 60)
    print(f"API URL: {API_URL}")
    print(f"Refresh Interval: {REFRESH_INTERVAL} seconds")
    print(f"SNS Enabled: {sns_client is not None}")
    print("=" * 60)
    print("EV Battery Critical Thresholds:")
    print("  Voltage: <300V or >410V")
    print("  Current: >120A")
    print("  Temperature: >65C")
    print("  State of Charge: <15%")
    print("  State of Health: <75%")
    print("=" * 60)
    print("Dashboard URL: http://localhost:5001")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5001)