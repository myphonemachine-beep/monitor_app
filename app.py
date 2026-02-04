


import json
import smtplib
import os
import requests
import threading
import platform
import subprocess
import atexit
from flask import Flask, render_template
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

# Load Credentials
GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASSWORD = os.getenv('GMAIL_PASSWORD')
ALERT_RECIPIENT = os.getenv('ALERT_RECIPIENT')

if GMAIL_PASSWORD:
    GMAIL_PASSWORD = GMAIL_PASSWORD.replace(" ", "")

MACHINES_FILE = 'machines.json'
STATUS_FILE = 'status_history.json'
PING_INTERVAL_SECONDS = 60  # <-- SET YOUR INTERVAL HERE

file_lock = threading.RLock()

# --- 2. HELPER FUNCTIONS ---

def load_json_file(filename, default=None):
    if default is None: default = []
    if not os.path.exists(filename):
        return default
    try:
        with file_lock:
            with open(filename, 'r') as f:
                return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return default

def save_json_file(filename, data):
    with file_lock:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4)

def ping_host(host):
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    command = ['ping', param, '1', host]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False

def create_email_html(alert_type, machine, url, time, details):
    if alert_type == 'DOWN':
        color = "#e74c3c" # Red
        title = "⚠️ Service Critical Alert"
        icon = "🔴"
        details_title = "Error Logs"
    else:
        color = "#27ae60" # Green
        title = "✅ Service Recovered"
        icon = "🟢"
        details_title = "Downtime Summary"

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; background-color: #f4f6f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.1);">
            <div style="background-color: {color}; padding: 20px; text-align: center; color: #ffffff;">
                <h1 style="margin: 0; font-size: 24px;">{icon} {title}</h1>
            </div>
            <div style="padding: 30px;">
                <p style="font-size: 16px; color: #555;">
                    The monitor has detected a status change for <strong>{machine}</strong>.
                </p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px; color: #888;">Machine Name:</td>
                        <td style="padding: 10px; font-weight: bold;">{machine}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px; color: #888;">Target:</td>
                        <td style="padding: 10px;"><a href="{url}" style="color: {color}; text-decoration: none;">{url}</a></td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px; color: #888;">Time:</td>
                        <td style="padding: 10px;">{time}</td>
                    </tr>
                </table>
                <div style="margin-top: 25px;">
                    <strong style="color: #333;">{details_title}:</strong>
                    <div style="background-color: #f8f9fa; border-left: 4px solid {color}; padding: 15px; margin-top: 10px; font-family: monospace; font-size: 13px; color: #333;">
                        {details}
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

def send_email(subject, html_body):
    msg = MIMEText(html_body, 'html')
    msg['Subject'] = subject
    msg['From'] = GMAIL_USER
    msg['To'] = ALERT_RECIPIENT
    try:
        print(f" -> Attempting to send email: {subject}...")
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
            print(f" -> [SUCCESS] Email sent to {ALERT_RECIPIENT}")
            return True
    except Exception as e:
        print(f" -> [ERROR] Failed to send email: {e}")
        return False

# --- 3. CORE LOGIC ---

def check_single_machine(machine):
    """Checks one machine and updates status."""
    name = machine['name']
    target = machine['url']
    check_type = machine.get('type', 'http')
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with file_lock:
        history = load_json_file(STATUS_FILE, default={})
        prev_data = history.get(
            name,
            {'status': 'Unknown', 'last_success': 'Never', 'last_offline': 'Never'}
        )

    current_status = "Unknown"
    error_msg = ""

    try:
        if check_type == 'ping':
            is_alive = ping_host(target)
            if is_alive: current_status = "Online"
            else:
                current_status = "Offline"
                error_msg = "Request Timed Out (Ping Failed)"
        else:
            response = requests.get(target, timeout=5)
            if response.status_code == 200: current_status = "Online"
            else:
                current_status = "Offline"
                error_msg = f"Status Code: {response.status_code}"
    except Exception as e:
        current_status = "Offline"
        error_msg = str(e)

    # ALERTS
    if current_status == 'Offline' and prev_data['status'] != 'Offline':
        print(f" [ALERT] {name} is DOWN!")
        email_html = create_email_html('DOWN', name, target, current_time, f"Type: {check_type}\nError: {error_msg}")
        send_email(f"🔴 ALERT: {name} is DOWN", email_html)
        prev_data['last_offline'] = current_time

    elif current_status == 'Online' and prev_data['status'] == 'Offline':
        print(f" [RECOVERY] {name} is back Online!")
        email_html = create_email_html('RECOVERY', name, target, current_time, f"Service is reachable.\nDowntime ended.")
        send_email(f"🟢 RECOVERY: {name} is ONLINE", email_html)
    
    if current_status == 'Online':
        prev_data['last_success'] = current_time

    prev_data['status'] = current_status
    prev_data['last_check'] = current_time
    
    with file_lock:
        full = load_json_file(STATUS_FILE, default={})

    if not isinstance(full, dict):
        full = {}

    full[name] = prev_data
    save_json_file(STATUS_FILE, full)

    return prev_data

def check_all_machines():
    """Wrapper to check all machines at once."""
    print(f"--- [Scheduled Check] Running at {datetime.now().strftime('%H:%M:%S')} ---")
    machines_config = load_json_file(MACHINES_FILE)
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(check_single_machine, machines_config))

# --- 4. FLASK ROUTES ---

@app.route('/')
def index():
    # Option 1: Just read the file (Faster page load, relies on scheduler)
    # machines_data = load_json_file(STATUS_FILE)
    # results = [{'name': k, **v} for k, v in machines_data.items()]

    # Option 2: Force a check NOW (Better for immediate feedback)
    machines_config = load_json_file(MACHINES_FILE)
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        # We need to manually construct the result list to pass to template
        futures = list(executor.map(check_single_machine, machines_config))
        # Merge config with status data
        for i, status_data in enumerate(futures):
            res = machines_config[i].copy()
            res.update(status_data)
            results.append(res)

    return render_template('index.html', machines=results)

# --- 5. STARTUP & SCHEDULER ---

if __name__ == '__main__':
    # Initialize Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_all_machines, trigger="interval", seconds=PING_INTERVAL_SECONDS)
    scheduler.start()
    
    # Ensure scheduler shuts down when app exits
    atexit.register(lambda: scheduler.shutdown())

    print(" -> Scheduler Started (Checks every 60s)")
    print(" -> Web Server Starting...")
    
    # Run Flask (use_reloader=False prevents scheduler from running twice in debug mode)
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)