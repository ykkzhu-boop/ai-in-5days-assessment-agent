import os
import sys
import sqlite3
import argparse
import uuid
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks
import uvicorn

from agent import ExpenseState, ExpenseAuditorAgent, logger, set_trace_context

# Create FastAPI app
app = FastAPI(title="Ambient Expense Agent Backend", version="2.0.0")

# SQLite Database setup
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claims.db")

def init_db():
    """Initializes SQLite database schemas for production claim storage."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            expense_id TEXT PRIMARY KEY,
            amount REAL,
            description TEXT,
            status TEXT,
            approver_notes TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_claim(claim: ExpenseState):
    """Inserts or updates a claim status inside the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO claims (expense_id, amount, description, status, approver_notes) VALUES (?, ?, ?, ?, ?)",
        (claim.expense_id, claim.amount, claim.description, claim.status, claim.approver_notes)
    )
    conn.commit()
    conn.close()

def get_claim(expense_id: str) -> Optional[dict]:
    """Retrieves a single claim details from the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT expense_id, amount, description, status, approver_notes FROM claims WHERE expense_id = ?", (expense_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "expense_id": row[0],
            "amount": row[1],
            "description": row[2],
            "status": row[3],
            "approver_notes": row[4]
        }
    return None

def get_all_claims_history() -> dict:
    """Retrieves claims history and aggregated compacted metadata summary."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT expense_id, amount, description, status, approver_notes FROM claims")
    rows = c.fetchall()
    claims = {}
    for row in rows:
        claims[row[0]] = {
            "expense_id": row[0],
            "amount": row[1],
            "description": row[2],
            "status": row[3],
            "approver_notes": row[4]
        }
    c.execute("SELECT value FROM metadata WHERE key = 'history_summary'")
    summary_row = c.fetchone()
    summary = summary_row[0] if summary_row else "No historical transactions processed yet."
    conn.close()
    return {"claims": claims, "history_summary": summary}

def background_compact_memory():
    """Aggregates transactional history in SQLite and writes a compacted summary to metadata."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT amount, status FROM claims")
    rows = c.fetchall()
    if len(rows) <= 5:
        conn.close()
        return
        
    total_approved = sum(1 for r in rows if r[1] == "approved")
    total_rejected = sum(1 for r in rows if r[1] == "rejected")
    total_pending = sum(1 for r in rows if r[1] == "pending")
    total_amount = sum(r[0] for r in rows if r[1] == "approved")
    
    summary = (
        f"Compacted History Summary (SQLite DB): Employee has submitted {len(rows)} total claims. "
        f"Approved: {total_approved} claims (Totaling ${total_amount:.2f}). "
        f"Rejected: {total_rejected} claims. Pending review: {total_pending} claims."
    )
    c.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('history_summary', ?)", (summary,))
    conn.commit()
    conn.close()
    logger.info(f"Background sqlite memory compaction executed: {summary}")

# Initialize database schemas on startup
init_db()

# API Input Model
class ProcessRequest(BaseModel):
    amount: float
    description: str

class ReviewRequest(BaseModel):
    action: str  # approved or rejected
    notes: Optional[str] = ""

# API Routing
@app.get("/")
def get_root():
    return {
        "status": "online",
        "service": "Ambient Expense Agent",
        "version": "2.0.0",
        "database": "SQLite Relational Database Persistent Connected",
        "endpoints": {
            "POST /": "Process an expense claim",
            "POST /process": "Process an expense claim",
            "GET /history": "Retrieve list of all processed claims",
            "POST /review/{expense_id}": "Submit human-in-the-loop review decision"
        }
    }

@app.get("/health")
def get_health():
    return {"status": "healthy"}

@app.post("/")
@app.post("/process")
def process_claim_endpoint(req: ProcessRequest, background_tasks: BackgroundTasks):
    trace_id = str(uuid.uuid4())
    span_id = str(uuid.uuid4())[:16]
    set_trace_context(trace_id, span_id)
    
    agent = ExpenseAuditorAgent()
    
    claim = ExpenseState(
        amount=req.amount,
        description=req.description
    )
    
    # Process claim through agentic graph pipelines
    processed_claim = agent.process_claim(claim)
    
    # Save claim state to SQLite DB
    save_claim(processed_claim)
    
    # Run async background memory compaction
    background_tasks.add_task(background_compact_memory)
    
    return processed_claim.dict()

@app.get("/history")
def get_history():
    return get_all_claims_history()

@app.post("/review/{expense_id}")
def review_claim_endpoint(expense_id: str, req: ReviewRequest):
    claim_dict = get_claim(expense_id)
    if not claim_dict:
        raise HTTPException(status_code=404, detail="Expense claim ID not found.")
        
    if claim_dict["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Claim is already resolved with status: {claim_dict['status']}")
        
    if req.action.lower() not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid action. Must be 'approved' or 'rejected'.")
        
    claim_state = ExpenseState(**claim_dict)
    claim_state.status = req.action.lower()
    claim_state.approver_notes = f"Human Auditor Decision: {req.action.capitalize()}. Notes: {req.notes}"
    
    save_claim(claim_state)
    logger.info(f"Human auditor resolved flagged claim {expense_id} as {req.action.upper()}")
    
    return claim_state.dict()


# ==========================================
# CLI Simulation Runner
# ==========================================
def run_cli_simulation():
    print("=" * 60)
    print("AMBIENT EXPENSE AUDITOR AGENT - SQLITE LOCAL RUN")
    print("=" * 60)
    
    agent = ExpenseAuditorAgent()
    
    # Test Case 1: Standard Claim (< $100)
    print("\n[Scenario 1] Processing standard claim: $45.00 for 'Team lunch at Tonkatsu'")
    claim1 = ExpenseState(amount=45.00, description="Team lunch at Tonkatsu")
    set_trace_context(str(uuid.uuid4()))
    result1 = agent.process_claim(claim1)
    save_claim(result1)
    print(f"Outcome Status: {result1.status.upper()}")
    print(f"Notes: {result1.approver_notes}")
    
    # Test Case 2: Large Claim (>= $100)
    print("\n[Scenario 2] Processing high-value claim: $250.00 for 'New office monitor'")
    claim2 = ExpenseState(amount=250.00, description="New office monitor")
    set_trace_context(str(uuid.uuid4()))
    result2 = agent.process_claim(claim2)
    save_claim(result2)
    print(f"Outcome Status: {result2.status.upper()}")
    print(f"Notes: {result2.approver_notes}")
    
    # Trigger Human-in-the-loop CLI interaction if flagged
    if result2.status == "pending":
        print("\n--- HUMAN IN THE LOOP TRIGGERED ---")
        print(f"Claim ID: {result2.expense_id}")
        print(f"Amount: ${result2.amount:.2f}")
        print(f"Description: {result2.description}")
        print(f"Auditor Note: {result2.approver_notes}")
        
        try:
            choice = input("\nApprove this flagged claim? (yes/no): ").strip().lower()
            if choice == "yes":
                result2.status = "approved"
                result2.approver_notes = "Approved manually by human auditor in console."
            else:
                result2.status = "rejected"
                result2.approver_notes = "Rejected manually by human auditor in console."
            save_claim(result2)
            print(f"\nFinal Cli Outcome: {result2.status.upper()}")
            print(f"Final Note: {result2.approver_notes}")
        except EOFError:
            print("\nNon-interactive mode: skipping manual entry input.")

    print("\n" + "=" * 60)
    print("Simulation execution complete.")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ambient Expense Agent")
    parser.add_argument("--cli", action="store_true", help="Run in local CLI simulation mode")
    args = parser.parse_args()
    
    if args.cli:
        run_cli_simulation()
    else:
        port = int(os.environ.get("PORT", 8080))
        logger.info(f"Starting FastAPI Web Server with SQLite persistence on port {port}")
        uvicorn.run(app, host="0.0.0.0", port=port)
