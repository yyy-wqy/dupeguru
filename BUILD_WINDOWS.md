# dupeGuru Windows 构建环境搭建（踩坑记录）

> 验证环境：Windows 11 + Anaconda Python 3.12.7 + Visual Studio Build Tools 2022（2026-09 验证通过）

dupeGuru 的图片版（`core/pe`）依赖 3 个 C 扩展（`_block`、`_cache`、`_block_qt`），
Windows 上必须用 MSVC 编译，且还有几个官方文档没提到的坑。本文记录一整套可直接复现的步骤。

## 0. 一句话版

```bash
python -m pip install -r requirements.txt            # 1. 装依赖
winget install --id Microsoft.VisualStudio.2022.BuildTools \
  --override "--wait --quiet --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"  # 2. 装编译器
python build.py                                      # 3. 完整构建
python run.py                                        # 4. 运行
```

## 1. 安装 Python 依赖

用 Python 3.12（Anaconda 或官方均可，**版本必须和最终运行一致**，见第 4 节）：

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-extra.txt    # 跑测试/打包才需要
```

## 2. 安装 MSVC 编译环境（关键）

C 扩展是 `cp3XX` 的 `.pyd`，必须用对应版本的 MSVC 编译。没装编译器会报：

```
error: Microsoft Visual C++ 14.0 or greater is required.
```

用 winget 一键装（会弹 UAC 确认，下载约 6–8 GB，耗时 20–40 分钟）：

```bash
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --silent \
  --accept-package-agreements --accept-source-agreements \
  --override "--wait --quiet --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

或者手动下载 [vs_BuildTools.exe](https://visualstudio.microsoft.com/visual-cpp-build-tools/)，勾选「使用 C++ 的桌面开发」。

装完验证：`C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe` 能查到 `isComplete: true`。

## 3. 完整构建

```bash
python build.py     # 编译 C 扩展 + 本地化 + Qt 资源 + 帮助文档
python run.py       # 启动 GUI
```

只做局部构建：

```bash
python build.py --modules   # 只重编 C 扩展
python build.py --loc       # 只重编本地化
```

## 4. 坑 1：pyrcc5 不在 PATH

`python build.py` 内部会调用 `pyrcc5` 生成 `qt/dg_rc.py`，但 Anaconda 的 PyQt5 不提供
`pyrcc5.exe` 脚本，导致构建在 Qt 资源那一步失败。

**方案 A（推荐）**：在 PATH 里的用户目录建一个 shim，例如 `C:\Users\<你>\bin\pyrcc5.bat`：

```bat
@echo off
python -m PyQt5.pyrcc_main %*
```

**方案 B（手动）**：绕过 `build.py` 的 Qt 步骤，手动生成：

```bash
python build.py --loc
python -m PyQt5.pyrcc_main qt/dg.qrc -o qt/dg_rc.py
python -c "from hscommon.build import fix_qt_resource_file; fix_qt_resource_file('qt/dg_rc.py')"
```

## 5. 坑 2：多 Python 共存导致 C 扩展 ABI 不匹配

C 扩展编译产物是 `_block.cp312-win_amd64.pyd` 这种名字，**`cp312` 只认 Python 3.12**。
如果机器上同时有 3.13 / 3.9 / 3.12 等多个 Python，务必全程用同一个 3.12 解释器：
装依赖、编译、运行都必须一致，否则会报：

```
ImportError: bad magic number / ModuleNotFoundError: No module named 'core.pe._block'
```

## 6. 坑 3：VS Code 按 F5 报 import 错误

VS Code 可能选中了别的 Python（比如没装 PyQt5 的 3.13）。在 `.vscode/settings.json` 里钉死解释器：

```json
"python.defaultInterpreterPath": "D:\\anaconda\\python.exe",
```

注意：如果之前**手动选过**别的解释器，`defaultInterpreterPath` 会被覆盖，需要
`Ctrl+Shift+P` → `Python: Select Interpreter` 重新选回 3.12（anaconda），
并确认右下角状态栏显示 `3.12.x`。

## 7. 测试

```bash
python -m pytest core hscommon   # 应全部通过（514 passed）
```
