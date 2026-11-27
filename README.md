# 一键启停工具

批量启动 / 关闭本地程序的 Windows 桌面工具。杨爽工作室出品。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 功能特性

- **一键运行 / 一键关闭**：启动所有勾选的条目，或关闭所有正在运行的程序（含子进程树）。
- **行级控制**：每条配置独立「运行」「关闭」按钮，状态列实时显示「运行中 / 已停止」。
- **本机启动项导入**：一键扫描系统自启动项并生成配置；支持禁止 / 恢复启动项（可逆备份）。
- **命令行接口**：`--run`、`--stop`、`--run-all`、`--stop-all`、`--list`，适合开机脚本与快捷方式。
- **零依赖**：纯 Python + tkinter + ctypes，无需安装任何第三方库即可运行。
- **单文件发布**：PyInstaller 打包为单个 exe（自带 UAC 管理员清单），双击即用，免安装。

## 安装使用

1. 下载 [OneKeyRunTool.exe](https://github.com/yangshuang9889/OneKeyRunTool/releases)（如有 Release）。
2. 双击打开界面，添加要管理的程序：名称、路径、参数、工作目录。
3. 勾选需要自动启动的条目，点「保存配置」。
4. 「一键运行」启动全部勾选项，「一键关闭」停止所有已启动项。

命令行用法示例：

```bat
OneKeyRunTool.exe --run "名称"        :: 启动指定项
OneKeyRunTool.exe --stop "名称"       :: 关闭指定项（含子进程）
OneKeyRunTool.exe --run-all           :: 一键运行全部勾选项
OneKeyRunTool.exe --stop-all          :: 一键关闭全部已启动项
OneKeyRunTool.exe --list              :: 列出配置与状态
```

## 开发指南

本项目使用 Python 3.11+ 标准库开发，无第三方依赖。

### 环境要求

- Windows 10 / 11
- Python 3.11 或以上（仅用于二次修改源码时）

### 打包说明

```bat
pip install pyinstaller==6.22.3
pyinstaller ^
    --onefile --windowed ^
    --uac-admin ^
    --icon "app.ico" ^
    --add-data "app.ico;." ^
    --name "OneKeyRunTool" ^
    --version-file version_info.txt ^
    OneKeyRunTool.py
```

版本信息通过 `version_info.txt` 注入（公司名 / 版权填「杨爽工作室」）。`--uac-admin`
会给 exe 嵌入 `requireAdministrator` 清单，双击即弹 UAC 以管理员身份启动，可直接操作
HKLM 启动项；若无该参数，程序仍可运行，但只对 HKCU 与用户启动文件夹生效。

详细构建步骤见 [开发手册.md](开发手册.md)。

## 目录结构

| 文件 | 作用 |
| --- | --- |
| `OneKeyRunTool.py` | Python 源码 |
| `OneKeyRunTool.exe` | 打包产物（需自行编译） |
| `version_info.txt` | 版本资源文件 |
| `app.ico` | 程序图标 |
| `使用说明.md` | 用户操作手册 |
| `开发手册.md` | 开发者文档 |
| `.gitignore` | Git 忽略规则 |

运行时数据（config.json、state.json、startup_backup.json 等）随 exe 同目录生成，不纳入仓库。

## 许可证

MIT License — [LICENSE](LICENSE)

Copyright (C) 2026 杨爽工作室
