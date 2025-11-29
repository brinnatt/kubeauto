"""
通用 hook - 自动处理所有包的依赖和入口点
"""
from PyInstaller.utils.hooks import collect_all, collect_entry_point
import pkg_resources

# 需要处理的包列表
PACKAGES_TO_HANDLE = [
    'ansible_runner',
    'taskflow',
    'stevedore',
]

datas = []
binaries = []
hiddenimports = []

"""
理论上collect_all(package)可以收集所有依赖
有时动态加载的组件不能被pyinstaller打包，像taskflow.engines，可以手动打包
if package == 'taskflow':
    for entry in pkg_resources.iter_entry_points('taskflow.engines'):
        hiddenimports.append(entry.module_name)
        print(f"✅ Added taskflow engine: {entry.module_name}")
等效于：
hiddenimports.extend(collect_entry_point('taskflow.engines'))
"""
for package in PACKAGES_TO_HANDLE:
    try:
        # 收集包的所有内容
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
        datas.extend(pkg_datas)
        binaries.extend(pkg_binaries)
        hiddenimports.extend(pkg_hiddenimports)
        print(f"✅ Collected {package}")
    except Exception as e:
        print(f"❌ Failed to handle {package}: {e}")

print(f"✅ Hook complete: {len(datas)} data files, {len(hiddenimports)} hidden imports")
