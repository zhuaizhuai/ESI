"""Server-side sessions, account lifecycle and project authorization."""
import hashlib
import hmac
import os
import secrets
import time
import uuid

from fastapi import HTTPException, Request, Response
from .core import connection, query

COOKIE = 'esi_session'
SESSION_SECONDS = 8 * 60 * 60
IDLE_SECONDS = 60 * 60
PASSWORD_ITERATIONS = 600_000
PUBLIC_USER = 'id,username,display_name,role,active,must_change_password,created'
PROJECT_ROLES = {'maintainer': 3, 'developer': 2, 'viewer': 1}
DATA_ROLES = {'owner': 3, 'analyst': 2, 'viewer': 1}


def init_auth():
    with connection() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
          role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          must_change_password INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
          csrf TEXT NOT NULL, expires REAL NOT NULL, last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_members (
          project_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
          PRIMARY KEY(project_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT,
          action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL,
          created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
          bucket TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_attempts_bucket ON login_attempts(bucket,created);
        CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS members_user ON project_members(user_id);
        CREATE INDEX IF NOT EXISTS events_task ON events(task_id,id);
        CREATE TABLE IF NOT EXISTS data_sources (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          path TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS data_source_members (
          source_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
          PRIMARY KEY(source_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS metrics (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', table_name TEXT NOT NULL,
          aggregation TEXT NOT NULL, value_column TEXT,
          date_column TEXT, dimensions TEXT NOT NULL DEFAULT '[]',
          created_by TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analysis_jobs (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
          question TEXT NOT NULL, report_kind TEXT NOT NULL,
          status TEXT NOT NULL, plan TEXT, query_spec TEXT,
          version INTEGER NOT NULL DEFAULT 0, approved INTEGER,
          created_by TEXT NOT NULL, approved_by TEXT, completed_by TEXT,
          result TEXT, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analysis_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          kind TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
          actor_id TEXT
        );
        CREATE INDEX IF NOT EXISTS data_members_user ON data_source_members(user_id);
        CREATE INDEX IF NOT EXISTS metrics_source ON metrics(source_id);
        CREATE INDEX IF NOT EXISTS analysis_source ON analysis_jobs(source_id,created);
        CREATE INDEX IF NOT EXISTS analysis_events_job ON analysis_events(job_id,id);
        ''')
        c.execute('BEGIN IMMEDIATE')
        # Additive migrations preserve all existing single-user data.
        for table, fields in {
            'projects': ['created_by'],
            'tasks': ['created_by', 'approved_by', 'completed_by'],
            'events': ['actor_id'],
        }.items():
            columns = {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
            for field in fields:
                if field not in columns:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {field} TEXT')


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}'


def verify_password(password, stored):
    try:
        algorithm, iterations, salt, expected = stored.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def audit(c, actor, action, target, detail=''):
    c.execute('INSERT INTO audit(actor_id,action,target,detail,created) VALUES(?,?,?,?,?)',
              (actor, action, target, detail, time.time()))


def get_session(request):
    token = request.cookies.get(COOKIE, '')
    if not token:
        raise HTTPException(401, '请登录')
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with connection() as c:
        row = c.execute('''SELECT u.*,s.csrf,s.token_hash FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires>? AND s.last_seen>? AND u.active=1''',
                        (token_hash, now, now-IDLE_SECONDS)).fetchone()
        if not row:
            raise HTTPException(401, '登录已过期或账号已停用')
        c.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (now, token_hash))
    return dict(row)


def current_user(request: Request):
    user = get_session(request)
    if user['must_change_password']:
        raise HTTPException(403, '请先修改初始密码')
    if request.method not in ('GET', 'HEAD'):
        supplied = request.headers.get('X-ESI-CSRF', '')
        if not hmac.compare_digest(supplied, user['csrf']):
            raise HTTPException(403, '请求校验失败，请刷新后重试')
    return user


def admin(user):
    if user['role'] != 'admin':
        raise HTTPException(403, '此操作需要系统管理员权限')


def require_project(c, user, pid, minimum='viewer'):
    # Re-read role/activity inside write transactions; never trust client role flags.
    account = c.execute('SELECT role,active,must_change_password FROM users WHERE id=?', (user['id'],)).fetchone()
    project = c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
    if not account or not account['active'] or account['must_change_password']:
        raise HTTPException(401, '账号已停用')
    if not project:
        raise HTTPException(404, '项目不存在或无权访问')
    membership = c.execute('SELECT role FROM project_members WHERE project_id=? AND user_id=?',
                           (pid, user['id'])).fetchone()
    role = 'maintainer' if account['role']=='admin' else membership['role'] if membership else None
    if not role:
        raise HTTPException(404, '项目不存在或无权访问')
    if PROJECT_ROLES[role] < PROJECT_ROLES[minimum]:
        raise HTTPException(403, '项目角色权限不足')
    return {**dict(project), 'my_role': role}


def require_data_source(c, user, source_id, minimum='viewer'):
    account = c.execute('SELECT role,active,must_change_password FROM users WHERE id=?', (user['id'],)).fetchone()
    source = c.execute('SELECT * FROM data_sources WHERE id=?', (source_id,)).fetchone()
    if not account or not account['active'] or account['must_change_password']:
        raise HTTPException(401, '账号已停用')
    if not source:
        raise HTTPException(404, '数据源不存在或无权访问')
    membership = c.execute('SELECT role FROM data_source_members WHERE source_id=? AND user_id=?',
                           (source_id, user['id'])).fetchone()
    role = 'owner' if account['role']=='admin' else membership['role'] if membership else None
    if not role:
        raise HTTPException(404, '数据源不存在或无权访问')
    if DATA_ROLES[role] < DATA_ROLES[minimum]:
        raise HTTPException(403, '数据源角色权限不足')
    return {**dict(source), 'my_role': role}


def public_user(user):
    return {key: user[key] for key in PUBLIC_USER.split(',')}


def start_session(c, response, uid):
    now = time.time()
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    c.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
              (hashlib.sha256(token.encode()).hexdigest(), uid, csrf, now+SESSION_SECONDS, now))
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=os.environ.get('ESI_COOKIE_SECURE','0')=='1', samesite='strict', path='/')
    return csrf


init_auth()


def require_admin(c, user):
    row=c.execute('SELECT role,active,must_change_password FROM users WHERE id=?',(user['id'],)).fetchone()
    if not row or not row['active'] or row['must_change_password'] or row['role']!='admin':
        raise HTTPException(403,'管理员授权已失效')
