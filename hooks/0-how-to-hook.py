"""
PyInstaller 的 hook 加载规则：
    hook-<package>.py - 自动为对应包加载
    hook-<dist>.py - 自动为对应分发加载

当 PyInstaller 分析到代码中有 import taskflow，它会自动去 hooks 目录查找 hook-taskflow.py 并执行：
    # Python导入语句              # 对应的hook文件名
    import ansible_runner   ->  hook-ansible_runner.py
    import taskflow         ->  hook-taskflow.py
    import stevedore        ->  hook-stevedore.py

PyInstaller 的 hook 自动处理：
    当你在 spec 中设置 hookspath=['hooks'] 时，PyInstaller 会：
        自动发现 hook - 找到所有 hook-<package>.py 文件
        自动执行 hook - 运行这些文件中的代码
        自动收集结果 - 将每个 hook 返回的 datas, binaries, hiddenimports 自动合并到最终的 Analysis 中
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
