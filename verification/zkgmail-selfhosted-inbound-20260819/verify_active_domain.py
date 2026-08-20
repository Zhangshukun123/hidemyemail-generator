import json, sqlite3, sys
key='zkgmail_qq_inbox_config_v1'
path=sys.argv[1]
conn=sqlite3.connect(path); row=conn.execute('select value from settings where key=?',(key,)).fetchone(); conn.close()
if row is None: raise SystemExit('missing setting')
print(json.loads(row[0])['activeDomain'])
