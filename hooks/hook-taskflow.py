"""
taskflow hook - 自动处理依赖和入口点
"""
from PyInstaller.utils.hooks import collect_all

print("🚀 hook-taskflow.py 开始执行!")

# 收集taskflow所有依赖
datas, binaries, hiddenimports = collect_all('taskflow')

print(f"✅ Collected taskflow: {len(datas)} data files, {len(hiddenimports)} hidden imports")
