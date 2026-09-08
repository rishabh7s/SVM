import sqlite3
from pathlib import Path

# Assuming you already set db_path to the correct file
db_path = Path('C:\\Users\\Rishabh Singh\\Downloads\\SVM\\db\\kivi.db') 

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Fetch every single row in the table
rows = conn.execute("SELECT * FROM episodic_events").fetchall()

print(f"Total events found: {len(rows)}\n")

for row in rows:
    print(dict(row))
