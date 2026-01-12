import mgclient
import sys

def try_connect(name, kwargs):
    print(f"--- Testing {name} ---")
    print(f"Params: {kwargs}")
    try:
        conn = mgclient.connect(**kwargs)
        conn.cursor().execute("RETURN 1")
        print("✅ SUCCESS!")
        conn.close()
        return True
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False

# 1. Try No Auth
if try_connect("NO AUTH", {"host": "localhost", "port": 7687}):
    sys.exit(0)

# 2. Try Default 'memgraph' / 'memgraph'
if try_connect("DEFAULT AUTH", {"host": "localhost", "port": 7687, "username": "memgraph", "password": "memgraph", "lazy": True}):
     sys.exit(0)

# 3. Try Found 'admin' / 'admin'
if try_connect("ADMIN AUTH", {"host": "localhost", "port": 7687, "username": "admin", "password": "admin"}):
     sys.exit(0)
