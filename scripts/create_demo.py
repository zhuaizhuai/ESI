"""创建完全本地、无需第三方依赖的验收仓库。"""
from pathlib import Path
import subprocess
root=Path(__file__).resolve().parents[1]/'runtime'/'demo-repo'
if root.exists():
    print('示例仓库已存在：',root)
    raise SystemExit(0)
(root/'tests').mkdir(parents=True)
(root/'calculator.py').write_text('def add(a, b):\n    return a + b\n')
(root/'tests'/'test_calculator.py').write_text('import unittest\nfrom calculator import add\n\nclass CalculatorTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')
(root/'README.md').write_text('# 示例计算器\n\n使用 Python 标准库，验证命令：python3 -m unittest discover -s tests\n')
subprocess.run(['git','init',str(root)],check=True)
subprocess.run(['git','-C',str(root),'add','.'],check=True)
subprocess.run(['git','-C',str(root),'-c','user.name=ESI Demo','-c','user.email=demo@localhost','commit','-m','Initialize demo'],check=True)
print('仓库路径：',root)
print('验证命令：python3 -m unittest discover -s tests')
print('需求：增加 subtract(a, b) 减法函数并补充测试，保持 add 的行为。')
