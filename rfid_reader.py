#!/usr/bin/env python3
import sys
import time
import requests
import threading
import RPi.GPIO as GPIO

# =============================================================================
# Configuration
# =============================================================================

# Use your Railway domain
BACKEND_BASE_URL = "https://smartaccesscontrol-backend-production.up.railway.app/api"

# Updated API endpoints
VERIFY_RFID_URL = f"{BACKEND_BASE_URL}/rfid/verify"
ACTIVATE_RFID_URL = f"{BACKEND_BASE_URL}/rfid/activate"
LOG_GRANTED_URL = f"{BACKEND_BASE_URL}/access-logs/granted"
LOG_DENIED_URL = f"{BACKEND_BASE_URL}/access-logs/denied"

RELAY_PIN = 17
UNLOCK_DURATION_SECONDS = 5
REQUEST_TIMEOUT = 5
LOG_THREAD_DAEMON = True

# =============================================================================
# GPIO Context Manager
# =============================================================================

class GPIOHandler:
    """
    Context manager for GPIO setup and teardown.
    """
    def __enter__(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        GPIO.cleanup()
        if exc_type:
            print(f"[ERROR] Exiting due to exception: {exc_type} - {exc_value}")
        else:
            print("[INFO] GPIO cleanup complete.")

# =============================================================================
# Network Session
# =============================================================================

session = requests.Session()  # Reuse HTTP session for efficiency

# =============================================================================
# Helper Functions
# =============================================================================

def unlock_door():
    """
    Unlocks the door (sets relay HIGH) for UNLOCK_DURATION_SECONDS.
    """
    print("[INFO] Unlocking door...")
    GPIO.output(RELAY_PIN, GPIO.HIGH)
    time.sleep(UNLOCK_DURATION_SECONDS)
    GPIO.output(RELAY_PIN, GPIO.LOW)
    print("[INFO] Door locked.")

def flash_relay(flash_count=6, interval=0.15):
    """
    Flashes the relay to indicate ACCESS DENIED.
    """
    print("[WARN] Flashing relay for access denial.")
    for _ in range(flash_count):
        GPIO.output(RELAY_PIN, GPIO.HIGH)
        time.sleep(interval)
        GPIO.output(RELAY_PIN, GPIO.LOW)
        time.sleep(interval)
    print("[WARN] Access denial flash complete.")

def log_access_attempt(endpoint, payload, success_message):
    """
    Logs an access attempt (granted or denied) asynchronously.
    """
    def log_worker():
        try:
            response = session.post(endpoint, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code == 201:
                print(f"[INFO] {success_message}")
            else:
                print(f"[ERROR] Logging failed: HTTP {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Logging request exception: {e}")
    t = threading.Thread(target=log_worker, daemon=LOG_THREAD_DAEMON)
    t.start()

def deny_access(rfid_uid, reason="Unknown reason"):
    """
    Denies access: flashes relay, logs the denial, and prints the reason.
    """
    print(f"[DENIED] ACCESS DENIED: {reason}")
    flash_relay()
    log_access_attempt(
        LOG_DENIED_URL,
        {"rfid_uid": rfid_uid},
        "Access denied logged successfully."
    )

def activate_rfid_if_assigned(rfid_uid, rfid_status):
    """
    If the RFID status is 'assigned', calls the backend to activate it (status -> 'active').
    Returns the updated RFID status or None on error.
    """
    if rfid_status != "assigned":
        return rfid_status

    print("[INFO] RFID status is 'assigned'. Attempting to activate...")
    try:
        response = session.post(
            ACTIVATE_RFID_URL,
            json={"rfid_uid": rfid_uid},
            timeout=REQUEST_TIMEOUT
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                updatedRFID = data.get("data", {})
                newStatus = updatedRFID.get("status", "unknown")
                print(f"[INFO] RFID {rfid_uid} successfully activated (status={newStatus}).")
                return newStatus
            else:
                print(f"[ERROR] Could not activate RFID {rfid_uid}: {data.get('message')}")
                return None
        else:
            print(f"[ERROR] Unexpected HTTP {response.status_code} activating RFID.")
            return None
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Cannot connect to backend to activate RFID: {e}")
        return None

def validate_rfid(rfid_uid):
    """
    1) Calls /rfid/verify to check if the RFID and room are valid.
    2) If success, activates the RFID if needed (assigned -> active) and unlocks the door.
    3) Denies access if the backend returns success: false.
    """
    print(f"[INFO] Sending verification request for RFID={rfid_uid}...")
    try:
        response = session.post(
            VERIFY_RFID_URL,
            json={"rfid_uid": rfid_uid},
            timeout=REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Cannot connect to backend: {e}")
        return

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError as e:
            print(f"[ERROR] JSON parse error: {e}")
            deny_access(rfid_uid, "Backend JSON parse error.")
            return

        print(f"[DEBUG] Full backend response: {data}")

        if not data.get("success"):
            deny_access(rfid_uid, data.get("message", "Unknown backend error."))
            return

        # Extract details
        var_data = data.get("data", {})
        rfid_data  = var_data.get("rfid", {})
        guest_info = var_data.get("guest", {})
        var_room   = var_data.get("room")

        # If room info is None, assign an empty dictionary to avoid errors
        room_info = var_room if var_room is not None else {}

        print(f"[INFO] RFID Verified: {rfid_uid}")

        if guest_info:
            print(f"[INFO] Guest Info => ID={guest_info.get('id')}, Name={guest_info.get('name')}")
        else:
            print("[WARN] No guest info provided by backend.")

        # Only print room info if available; otherwise, log a warning.
        if room_info:
            print("[INFO] Room Info => "
                  f"ID={room_info.get('id')}, "
                  f"Number={room_info.get('room_number')}, "
                  f"Status={room_info.get('status')}, "
                  f"check_in={room_info.get('check_in')}, "
                  f"check_out={room_info.get('check_out')}")
        else:
            print("[WARN] No room info provided by backend.")

        # Activate RFID if still assigned.
        current_status = rfid_data.get("status")
        updated_status = activate_rfid_if_assigned(rfid_uid, current_status)
        if updated_status is None:
            deny_access(rfid_uid, f"RFID {rfid_uid} could not be activated.")
            return

        # Unlock the door.
        unlock_door()

        # Log successful access.
        payload = {
            "rfid_uid": rfid_uid,
            "guest_id": guest_info.get("id") if guest_info else None
        }
        log_access_attempt(LOG_GRANTED_URL, payload, "Access granted logged successfully.")

    elif response.status_code in (403, 404):
        try:
            reason = response.json().get("message", "No detail provided")
        except ValueError:
            reason = "No detail provided"
        deny_access(rfid_uid, reason)
    else:
        print(f"[ERROR] Unexpected backend response: HTTP {response.status_code}")
        deny_access(rfid_uid, f"Unexpected status {response.status_code}")

def main():
    print("[INFO] RFID Reader is active. Waiting for scans (Ctrl+C to exit).")
    try:
        while True:
            rfid_uid = input().strip()
            if not rfid_uid:
                continue
            print(f"[SCAN] RFID Scanned: {rfid_uid}")
            validate_rfid(rfid_uid)
    except KeyboardInterrupt:
        print("\n[INFO] Exiting RFID reader gracefully...")
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
    finally:
        sys.exit(0)

if __name__ == "__main__":
    with GPIOHandler():
        main()
