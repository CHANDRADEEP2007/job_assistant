import os
from dotenv import load_dotenv

load_dotenv()

# API Keys and Paths
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "")

# Strategic Guardrails
MATCH_SCORE_THRESHOLD = 65

# File Paths
RESUME_PATH = "Resume.pdf"
PROFILE_PATH = "profile.json"

# The 4 Excel Tracking Files
LEDGER_PATH = "Knowledge_Ledger.xlsx"
APPLIED_APPS_PATH = "Applied_Applications.xlsx"
ALL_APPS_PATH = "All_Applications.xlsx"
CREDENTIALS_PATH = "Credentials.xlsx"
