"""Validation runtime: container isolation by default, explicit local opt-in."""
import os
import shutil
import subprocess
import uuid
from pathlib import Path


def mode():
    return os.environ.get('ESI_EXECUTION_MODE','docker')


def status():
    selected=mode()
    available=selected=='local' or (selected=='docker' and shutil.which('docker') is not None)
    return {'mode':selected,'available':available,
            'image':os.environ.get('ESI_EXECUTION_IMAGE','python:3.12-slim'),
            'message':'本地开发执行：仅限完全可信团队' if selected=='local' else
                      'Docker 就绪情况在任务执行前检查' if available else '服务器未安装 Docker，代码执行暂不可用'}


def preflight():
    if mode()=='local':
        return
    if mode()!='docker':
        raise RuntimeError('ESI_EXECUTION_MODE 必须为 docker 或 local')
    if not shutil.which('docker'):
        raise RuntimeError('团队模式需要 Docker 验证环境；请由管理员安装并准备执行镜像')
    image=os.environ.get('ESI_EXECUTION_IMAGE','python:3.12-slim')
    try:
        subprocess.run(['docker','image','inspect',image],check=True,capture_output=True,timeout=15)
    except (subprocess.SubprocessError,OSError):
        raise RuntimeError('Docker 不可用或执行镜像不存在，请由管理员预先准备镜像：'+image)


def command_spec(root, command):
    if mode()=='local':
        return ['/bin/sh','-lc',command],None
    if mode()!='docker':
        raise RuntimeError('未知执行模式')
    root=Path(root).resolve()
    name='esi-check-'+uuid.uuid4().hex[:16]
    args=['docker','run','--pull','never','--rm','--name',name,
          '--network','none','--read-only','--cap-drop','ALL',
          '--security-opt','no-new-privileges','--pids-limit','128',
          '--memory','512m','--memory-swap','512m','--cpus','1',
          '--user',f'{os.getuid()}:{os.getgid()}',
          '--tmpfs','/tmp:rw,nosuid,nodev,size=128m',
          '--mount',f'type=bind,src={root},dst=/workspace',
          '--mount',f'type=bind,src={root / ".git"},dst=/workspace/.git,readonly',
          '--workdir','/workspace','--env','HOME=/tmp','--env','PYTHONDONTWRITEBYTECODE=1',
          os.environ.get('ESI_EXECUTION_IMAGE','python:3.12-slim'),'/bin/sh','-lc',command]
    return args,name


def cleanup(name):
    if name:
        try:
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=10)
        except (subprocess.SubprocessError,OSError):
            pass
