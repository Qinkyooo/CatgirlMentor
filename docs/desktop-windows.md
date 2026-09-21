# CatgirlMentor for Windows

房区、钓鱼与市场工具支持有限的自动恢复：网站压缩变量改名时按字段结构识别数据；
攻略拆分后跟随当前网站清单中的鱼种和更新时间选择文件；房区脚本地址从页面重新发现，
展示数据缓存每五分钟重新检查；临时连接超时或断开自动重试一次。
服务器、价格、抽签阶段和地区对应关系仍严格校验，无法确认的数据会返回明确错误，
不会猜测或让模型直接修改程序。网站更换接口协议或字段含义仍可能需要更新应用。

打包会自动检查 FF14 服务、服务器表、攻略库和时区能否加载。发布前再运行真实数据源检查
（只读取公共网站，不调用模型，也不读取用户配置）：

```powershell
dist/windows-tools-fix/CatgirlMentor/runtime/python.exe scripts/smoke_ffxiv.py --live
```

## 快速开始

1. 双击 `CatgirlMentor-0.3.5-Windows-x64-Setup.exe`，安装到当前用户目录，无需管理员权限。
2. 从桌面或开始菜单打开 CatgirlMentor，在“模型配置”中填写服务商和 API 密钥，点击“获取模型”后从下拉列表选择，也可以手动输入模型名称。
3. 点击“保存并测试连接”，成功后回到“概览”，点击“启动后台”，再打开聊天界面。
4. 关闭浏览器不会退出应用。通过系统托盘的书本图标重新打开管理页；需要结束时选择“退出应用”。
5. 在“应用设置”中按需开启登录 Windows 后自动启动。卸载会保留配置、会话和工作目录。

安装向导和管理界面均为中文，聊天界面首次打开默认简体中文；已有语言选择会保留。此版本是 Windows 10/11 x64 预览版，安装包尚未签名。

“应用设置 → 更换数据来源”用于选择本应用的数据或以前 nanobot 的数据。
两套模型配置和会话各自保留，切换不会复制或删除数据；请先停止后台。
获取模型列表使用当前表单的连接信息，不会自动保存配置。服务商不支持列表时可手动输入。

Windows 10/11 x64 preview application. The installer includes Python, dependencies,
the existing chat WebUI, QQ SDK and FF14 knowledge resources. End users do not need Python,
Node.js, Git or a terminal. External model/data services still require connectivity.

## Use

Run `CatgirlMentor-<version>-Windows-x64-Setup.exe`. Install for the current user,
then launch the desktop or Start menu shortcut. Fill the model provider, model name
and API key, save or test the connection, and start the gateway from Overview.

The independent manager remains available when the gateway is stopped or config is
invalid. The encyclopedia tray icon opens it again. Closing the browser leaves the
gateway running. “退出应用” stops only the gateway owned by this application.
An external CLI gateway is shown as external and must be stopped from its original
entry point. Login startup is opt-in under Application settings; it runs silently.

New data lives in `%LOCALAPPDATA%\CatgirlMentor`; choosing the existing configuration
uses `%USERPROFILE%\.nanobot\config.json` without copying or replacing it. The settings
page displays the actual path. Reset first renames the config to a timestamped backup.
Application files normally live in `%LOCALAPPDATA%\Programs\CatgirlMentor`.
Uninstall removes application files and its startup entry, while retaining user data.
Upgrade uses the same AppId and first requests a graceful application shutdown.
Legacy lowercase `catgirlmentor` default installation/data folders and executable
are renamed to `CatgirlMentor` casing after shutdown; custom installation folders
keep their location. Existing configuration, API keys, workspace/history and the
login-startup selection are retained without rewriting user configuration.

The quick form covers API-key providers. Existing OAuth, named model presets,
channels, tools and advanced settings continue through the chat WebUI. Saving the
quick form switches the active model to the selected provider/model, retaining other
saved presets. Optional channel SDKs and external MCP runtimes are not all bundled;
they must be available for the corresponding optional integrations.

## Build

Build on Windows x64 with uv, Bun, .NET Framework compiler and Inno Setup 6.7.3.
The developer machine needs those tools; the installed application does not.
Acquire `python-3.14.7-embed-amd64.zip` from the Python Software Foundation and verify
the signature on `python.exe`; this build was validated with that exact runtime.

```powershell
uv export --extra desktop --no-dev --no-emit-project --format requirements-txt --output-file packaging/windows/requirements.txt
python scripts/build_desktop.py --output dist/windows-release --python-zip C:/build-tools/python-3.14.7-embed-amd64.zip --iscc 'C:/build-tools/inno/ISCC.exe'
```

The requirements file pins dependency versions and hashes. Regenerate it
only when deliberately updating dependencies. Use a fresh output directory for each
build. The build verifies required assets, emits per-file SHA256 in
`CatgirlMentor/build-manifest.json`, and creates a current-user installer.

## 更新源码后重新打包

打包流程由 `scripts/build_desktop.py` 记录，安装和升级规则由
`packaging/windows/CatgirlMentor.iss` 记录。管理界面、桌面图标和托盘资源在
`nanobot/desktop/`；聊天默认中文的设置在 `webui/src/i18n/config.ts` 和
`webui/index.html`。这些都是源码，不依赖本次聊天记录，也不需要从旧安装包提取。

正式打包分支为 `codex/windows-packaging`，在 `D:\gamebot\nanobot` 中使用。
桌面源码、图标资源、测试、固定依赖和打包规则随此分支提交保存；`main` 保持原样。
原来的 `catgirlmentor-app` 是同一仓库的关联工作树，保留在开发分支
`codex/catgirlmentor-windows` 上。安装包等构建产物位于被 Git 忽略的 `dist/` 中。
本地提交不会自动上传远程，远程备份需另行推送。

更新 `main` 后，在工作区没有未提交修改的情况下，把更新合入打包分支：

```powershell
cd D:/gamebot/nanobot
git switch codex/windows-packaging
git merge main
```

这个方向只更新打包分支，不会把桌面功能合回 `main`。解决冲突并验证后再重新打包。

后续流程：

1. 在保留桌面功能的分支上合并新版 nanobot 源码，处理冲突；不要用新版源码整目录覆盖当前目录。
2. 检查模型配置、网关生命周期和 WebUI 接口兼容性。如果依赖发生变化，重新导出
   `packaging/windows/requirements.txt` 并检查更新内容；其余情况下沿用固定依赖。
3. 按发布需要更新 `pyproject.toml` 中的版本号，运行相关测试。
4. 使用上面的构建命令，给 `--output` 指定一个全新的目录。
   源码或聊天界面更新后不要使用 `--webui-built`，让脚本重新构建 WebUI。
5. 运行打包后的冒烟测试，并在测试安装目录验证升级，再分发新安装包。

普通重新打包直接复用已保存的图标文件。只有要重新导出图标时才需要原始设计图。
构建机仍需准备 uv、Bun、Inno Setup、.NET 编译器和指定的 Python 嵌入包；
这些工具与源码分开保存，并不会安装到最终用户的开发环境中。

用户配置、密钥和会话保存在程序目录之外，当前安装器升级时保留这些数据。
保持安装器 AppId 和数据目录约定不变，才能继续识别为同一应用。
如果新版 nanobot 改变配置格式，仍需验证迁移兼容性。
`scripts/export_desktop_icons.py` converts the approved portrait and renders the flat
encyclopedia geometry into individual PNG sizes and ICO. It does not crop the A/B board.

The vendored `ChineseSimplified.isl` comes from the translation linked by Inno Setup's
[translation directory](https://jrsoftware.org/files/istrans/) and retains its author attribution.

The manager uses static HTML/CSS/JS and the approved cream/leaf-green palette, without
the decorative hero from the concept. Desktop portrait reference: `nanobot-desktop-icon-soft-v2.png`.
Tray sizes: 16, 20, 24, 32, 40, 48 pixels; native system menus.

## Verification

2026-09-21 修复验证：桌面、网站更新兼容与市场匹配共 46 项测试通过；Ruff 与相关模块严格类型检查通过。
使用新包内运行环境实测红龙、波太郎攻略、鱼窗、天气、市场价格及房区空房、详情、推荐均成功。
管理器通过本地模型测试服务完成配置、模型列表、连接测试、中文聊天页面、启动、重启、停止与退出检查。
另用隔离 AppId、注册表键和用户目录验证旧版覆盖升级与卸载：实际目录大小写已统一，
配置、测试密钥和会话逐字节保留，自启动选择保留，旧运行环境清理完成。

```powershell
python -m pytest tests/desktop tests/gateway tests/cli/test_gateway_commands.py tests/cli/test_gateway_runtime.py -q
python scripts/smoke_desktop.py --python dist/windows-release/CatgirlMentor/runtime/python.exe --data-dir C:/test-data/new-smoke-directory
```

The smoke script uses a local model stub, no real credentials or paid calls. It
exercises an actual manager process and gateway, config save, connection test, WebUI
HTML load, restart, stop and shutdown. Test data must be new and is retained for diagnosis.

Local validation on 2026-09-20: 106 tests passed, 3 skipped in the targeted desktop /
gateway / CLI suite; Ruff passed; strict BasedPyright reported zero errors for
`nanobot/desktop` and `nanobot/gateway/runtime.py`. Both source and bundled Python
passed the local-model smoke flow. Browser checks covered configuration save,
secret-field clearing, status/settings rendering and absence of console errors.
The native launcher/tray, single instance and installer upgrade were exercised with
an isolated profile; upgrade removed a deliberately planted obsolete runtime module.
The final installer also passed uninstall verification: runtime bytecode caches and
the installation registration were removed while the user-data sentinel remained.

Chinese/UI refinement: 22 desktop tests and 27 i18n tests passed; model-list lookup
is checked against a local HTTP stub without saving draft configuration. Provider
changes clear draft credentials; changing an endpoint requires explicitly re-entering
a saved key before model discovery. Browser checks covered real dropdown selection,
data-source switching, draft clearing, and the CatgirlMentor portrait header.

Before public release, validate a clean Windows 10 and Windows 11 VM, actual logout /
login startup, light/dark taskbar at different DPI settings, and installer upgrade
from the previous public version. The generated installer is unsigned until a release
signing certificate is configured; a successful local build is not a substitute for
those release checks.
