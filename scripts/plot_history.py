import sqlite3, pandas as pd, matplotlib.pyplot as plt

conn = sqlite3.connect("farewatch.db")
df = pd.read_sql("""
SELECT w.origin, w.destination, w.depart_date, s.seen_utc, s.price_cents/100.0 AS price
FROM fare_snapshots s JOIN watches w ON w.id=s.watch_id
ORDER BY w.depart_date, s.seen_utc
""", conn)

for (route, dep), group in df.groupby(["destination","depart_date"]):
    plt.plot(pd.to_datetime(group["seen_utc"]), group["price"], label=dep)

plt.title("BWI→SFO Price History")
plt.xlabel("Snapshot Time")
plt.ylabel("Price (USD)")
plt.show()