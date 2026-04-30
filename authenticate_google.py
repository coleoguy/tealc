"""Run this once to authorize Tealc to access Gmail, Calendar, Drive, Docs, and Sheets."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

CREDS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "data", "google_token.json")

if not os.path.exists(CREDS_PATH):
    print(f"ERROR: {CREDS_PATH} not found.")
    sys.exit(1)

os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)

flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
creds = flow.run_local_server(port=8080)

with open(TOKEN_PATH, "w") as f:
    f.write(creds.to_json())

print(f"\nSuccess! Token saved to {TOKEN_PATH}")
print("Tealc now has full Gmail, Calendar, Drive, Docs, and Sheets access.")
