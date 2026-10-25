"""
Background worker scaffold for the Meesho Shipping Checker SaaS.

This worker deliberately does NOT log into Meesho or capture shipping charges yet.
A per-user authenticated Browserless session and verified selectors must be implemented
before it can safely process jobs. It reports its health and leaves jobs untouched.
"""
import os
import time
from app import app, db, Job

POLL_SECONDS = max(2, int(os.environ.get("WORKER_POLL_SECONDS", "10")))

def main():
    print("Meesho worker scaffold started. Real capture is disabled until per-user sessions/selectors are configured.")
    while True:
        with app.app_context():
            # Do not claim queued jobs are processed; report pending jobs for deployment diagnostics.
            queued = Job.query.filter_by(status="queued").count()
            if queued:
                print(f"{queued} queued job(s) waiting. No capture performed: per-user Meesho session is not configured.")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
