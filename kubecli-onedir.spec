import sys
from pathlib import Path


# 如果需要对 pyinstaller 打包内容加密，这里可以放置密码。通常不用。
block_cipher = None


# 在 .spec 文件里不要用 __file__，而是用 spec 提供的上下文变量。
# PyInstaller 在执行 .spec 时，会把当前 spec 文件的目录放到 SPECPATH 里。
project_root = Path(SPECPATH)


# 扫描入口文件和依赖，生成依赖清单（Python 模块、数据文件等）
a = Analysis(
    # 入口脚本（你项目的 CLI 主程序）
    ['kubecli.py'],

    # 搜索路径（告诉 pyinstaller 在这个目录下找包）
    pathex=[str(project_root)],

    # 额外的二进制依赖（.so / .dll），这里为空。PyInstaller 会自动找常见的。
    binaries=[],

    # 非 Python 文件（模板、yaml、图片、插件库等）要手动加进来。
    datas=[
        ('playbooks', 'playbooks'),
        ('roles', 'roles'),
        ('common', 'common'),
        ('pics', 'pics'),
        ('/usr/local/lib/python3.12/site-packages/ansible_runner', 'ansible_runner')
    ],

    # PyInstaller 在分析依赖时，有些模块是动态 import 的（比如 __import__()、插件加载、Jinja2 filters 之类），静态分析抓不到。
    # 别管分析到没分析到，这个模块必须强制打包进去。
    hiddenimports=[],

    # 自定义 hook 文件目录，通过hooks库中的方法把某些库的缺陷弥补上，比如ansible_runner的插件可以通过这里收集进去
    hookspath=[],

    # 运行时 hook，在 EXE 运行前执行，设置环境变量（例如 ANSIBLE_ROLES_PATH）。
    runtime_hooks=['runtime_hook.py'],

    # 不想打包的模块可以写在这里。
    excludes=[],

    # 加密用的密码对象（一般不用）。
    cipher=block_cipher,
)

# 把纯 Python 代码（.py/.pyc）打包成一个 zip 归档，运行时直接从内存加载。
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# 生成最终的可执行文件（只是 exe 本体，不包含依赖文件）
exe = EXE(
    # Python 归档
    pyz,

    # 入口脚本（Analysis 阶段收集到的）
    a.scripts,

    # 这里可以附加额外脚本（很少用），留空即可。
    [],

    # 是否在这里排除二进制文件。设为 True，意味着让 COLLECT 阶段统一收集。
    exclude_binaries=True,

    # 最终可执行文件名（不带扩展名）。Windows 会生成 kubecli.exe。
    name='kubecli',

    # 是否启用调试（会有更多日志）。生产环境一般 False。
    debug=False,

    # 是否忽略操作系统信号，通常保持 False。
    bootloader_ignore_signals=False,

    # 是否用 strip 去掉调试符号，减小体积。Linux 下常用。
    strip=False,

    # 是否用 UPX 压缩可执行文件（需要系统安装 upx 工具）。
    upx=True,

    # 信息输出到控制台
    console=True,
)


# 最后一步，把 EXE、依赖库、数据文件统一收集到 dist/kubecli/ 目录下。
coll = COLLECT(
    # 生成的可执行文件
    exe,

    # 二进制依赖（.dll / .so）
    a.binaries,

    # pyinstaller 打包的 zip 文件（pyz）
    a.zipfiles,

    # 数据文件（playbooks、roles、common、pics 等）
    a.datas,

    # 是否 strip（同上）
    strip=False,

    # 是否 UPX 压缩（同上）
    upx=True,

    # 最终输出目录名 dist/kubecli/
    name='kubecli',
)