"""Run from any directory; .env remains server-local and is not logged."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.umask(0o077)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
import uvicorn

load_dotenv(ROOT / ".env", override=False)
if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8894")), workers=1,
                timeout_graceful_shutdown=30, access_log=False)
