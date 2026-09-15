"""Offline initial administrator provisioning. Never exposes a public setup API."""
import secrets
import uuid
import time
import os
from .core import connection, DATA
from .auth import password_hash, audit


def bootstrap():
    password = secrets.token_urlsafe(24)
    credential_file = DATA / 'bootstrap-admin.txt'
    uid = uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            print('账号已初始化，不会覆盖管理员或密码。')
            return
        # Exclusive creation, mode 0600; no plaintext password in stdout or logs.
        fd = os.open(credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                      (uid,'admin','系统管理员',password_hash(password),'admin',1,1,time.time()))
            c.execute('UPDATE projects SET created_by=? WHERE created_by IS NULL', (uid,))
            c.execute('UPDATE tasks SET created_by=? WHERE created_by IS NULL', (uid,))
            c.execute("INSERT OR IGNORE INTO project_members SELECT id,?,'maintainer' FROM projects", (uid,))
            # Legacy approvals lacked identity: require a new approval before execution.
            c.execute("UPDATE tasks SET status='awaiting_approval',approved=NULL WHERE status='queued' AND approved_by IS NULL")
            audit(c, uid, 'bootstrap', uid, '初始化管理员并接管历史项目；历史审批须重新确认')
            with os.fdopen(fd,'w') as f:
                f.write('ESI 初始管理员\n用户名：admin\n临时密码：'+password+'\n\n首次登录必须修改密码，修改后此文件自动清除。\n')
        except BaseException:
            credential_file.unlink(missing_ok=True)
            raise
    print('管理员已初始化。初始凭据只写入本机文件：'+str(credential_file))

if __name__ == '__main__':
    bootstrap()
