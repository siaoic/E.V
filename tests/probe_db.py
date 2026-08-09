import sqlite3, os, glob

con = sqlite3.connect("data/memu.sqlite3")
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print("tables:", tables)
for t in tables:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"{t}: {cur.fetchone()[0]} 行")
    except Exception as e:
        print(t, "ERR", e)
con.close()

print("\ndata/backup:")
for p in glob.glob("data/backup/*"):
    print(" ", p, os.path.getsize(p))
