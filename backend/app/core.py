import json, os, sqlite3, subprocess, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get('ESI_DATA', str(ROOT / 'runtime'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'esi.db'

def connection():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init():
    with connection() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, path TEXT, branch TEXT, checks TEXT);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project_id TEXT, title TEXT, requirement TEXT, acceptance TEXT, status TEXT, plan TEXT, version INTEGER DEFAULT 0, approved INTEGER, worktree TEXT, base TEXT, result TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, message TEXT, created REAL);
        ''')

def query(sql, args=(), one=False):
    with connection() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows

def execute(sql, args=()):
    with connection() as c: c.execute(sql, args)

def event(tid, message, kind='info', actor_id=None):
    execute('INSERT INTO events(task_id,kind,message,created,actor_id) VALUES(?,?,?,?,?)', (tid,kind,str(message)[:16000],time.time(),actor_id))

def update(tid, **values):
    with connection() as c:
        changed=c.execute('UPDATE tasks SET '+','.join(k+'=?' for k in values)+
            " WHERE id=? AND status NOT IN ('cancelled','interrupted')", (*values.values(),tid))
        if changed.rowcount and 'status' in values:
            from .platform import sync_workflow
            sync_workflow(c,'development',tid,values['status'])

def git(path, *args):
    return subprocess.check_output(['git','-C',str(path),*args],stderr=subprocess.STDOUT,text=True,timeout=30).strip()

def safe_path(root, name):
    root = Path(root).resolve()
    p = (root / name).resolve()
    if p != root and root not in p.parents: raise ValueError('文件路径超出工作区')
    relative = p.relative_to(root)
    if any(x in {'.git','node_modules','.venv'} or x == '.env' or x.startswith('.env.') or x.endswith(('.pem','.key')) for x in relative.parts): raise ValueError('禁止访问此路径')
    return p

def files(root):
    found=[]
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {'.git','node_modules','.venv','dist','__pycache__'} and not Path(base,d).is_symlink()]
        for name in names:
            p=Path(base,name)
            if name == '.env' or name.startswith('.env.') or name.endswith(('.pem','.key')) or p.is_symlink(): continue
            found.append(str(p.relative_to(root)))
            if len(found)>=1500: return found
    return found

init()
