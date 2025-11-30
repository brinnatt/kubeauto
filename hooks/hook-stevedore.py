"""
stevedore hook - 自动处理依赖
"""
from PyInstaller.utils.hooks import collect_all

print("🚀 hook-stevedore.py 开始执行!")

datas, binaries, hiddenimports = collect_all('stevedore')

print(f"✅ Collected stevedore: {len(datas)} data files, {len(hiddenimports)} hidden imports")