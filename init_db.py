from pathlib import Path
from db import connect, init_db

DB_PATH = Path(r"D:\AlgoAlps\algoalps.db")

conn = connect(DB_PATH)
init_db(conn)
print("Initialized:", DB_PATH)
