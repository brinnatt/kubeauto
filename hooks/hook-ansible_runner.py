"""
ansible_runner hook - 自动处理依赖
"""
from PyInstaller.utils.hooks import collect_all

print("🚀 hook-ansible_runner.py 开始执行!")

datas, binaries, hiddenimports = collect_all('ansible_runner')

print(f"✅ Collected ansible_runner: {len(datas)} data files, {len(hiddenimports)} hidden imports")