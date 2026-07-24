import os
import sqlite3
import json
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "migration_journal.db"
BACKUP_DIR = "backups"

def init_safety_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Main Migration Journal
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS migration_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        record_id INTEGER NOT NULL,
        column_name TEXT NOT NULL,
        original_url TEXT NOT NULL,
        new_pcloud_url TEXT NOT NULL,
        pcloud_path TEXT NOT NULL,
        r2_key TEXT,
        state TEXT NOT NULL DEFAULT 'discovered',
        sha1_hash TEXT,
        size_bytes INTEGER,
        error_log TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # 2. Full Pre-Migration Row Backup Snapshots
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS row_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        record_id INTEGER NOT NULL,
        full_row_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    conn.commit()
    conn.close()
    print("Initialized local safety database: migration_journal.db")

def save_row_snapshot(batch_id, table_name, record_id, row_dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Save into SQLite
    cursor.execute("""
    INSERT INTO row_snapshots (batch_id, table_name, record_id, full_row_json)
    VALUES (?, ?, ?, ?)
    """, (batch_id, table_name, record_id, json.dumps(row_dict, default=str)))
    
    # Also save as local JSON snapshot file in backups/
    snapshot_filename = os.path.join(BACKUP_DIR, f"{batch_id}_{table_name}_{record_id}.json")
    with open(snapshot_filename, "w", encoding="utf-8") as f:
        json.dump(row_dict, f, indent=2, default=str)
        
    conn.commit()
    conn.close()

def rollback_batch(batch_id):
    """
    Restores all PostgreSQL rows mutated in a given batch back to their original URLs.
    """
    pg_url = os.getenv("PG_PROXY_URL")
    pg_token = os.getenv("PG_PROXY_AUTH_TOKEN")
    pg_db = os.getenv("PG_PROXY_DB_NAME", "prod_main")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
    SELECT id, table_name, record_id, column_name, original_url, new_pcloud_url, state
    FROM migration_journal
    WHERE batch_id = ? AND state IN ('db_updated', 'deleting', 'completed', 'cleanup_pending')
    """, (batch_id,))
    
    rows = cursor.fetchall()
    if not rows:
        print(f"No mutated records found for batch '{batch_id}' to rollback.")
        conn.close()
        return
        
    print(f"\n--- INITIATING ROLLBACK FOR BATCH '{batch_id}' ({len(rows)} items) ---")
    reverted_count = 0
    
    for item in rows:
        journal_id, table_name, record_id, column_name, original_url, new_pcloud_url, current_state = item
        
        # Execute PostgreSQL Compare-and-set update to restore original_url
        sql = f"UPDATE {table_name} SET {column_name} = %s WHERE id = %s AND {column_name} = %s;"
        resp = requests.post(
            f"{pg_url.rstrip('/')}/api/sql",
            headers={"Authorization": f"Bearer {pg_token}", "Content-Type": "application/json"},
            json={
                "db_name": pg_db,
                "sql": f"UPDATE {table_name} SET {column_name} = $1 WHERE id = $2 AND {column_name} = $3;",
                "params": [original_url, record_id, new_pcloud_url]
            }
        )
        
        if resp.status_code == 200 and resp.json().get("rowCount", 0) == 1:
            cursor.execute("UPDATE migration_journal SET state = 'reverted', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (journal_id,))
            print(f"[REVERTED] Record #{record_id} ({table_name}.{column_name}) restored to original URL.")
            reverted_count += 1
        else:
            print(f"[REVERT FAILED] Record #{record_id}: {resp.text}")
            
    conn.commit()
    conn.close()
    print(f"\nRollback finished. {reverted_count}/{len(rows)} records restored.")

if __name__ == "__main__":
    init_safety_db()
