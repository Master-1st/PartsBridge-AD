# PartsBridge AD 0.3.4 验证说明

验证日期：2026-08-26。环境：Windows 11 x64、Python 3.12.10、PyInstaller 6.22.2。

## 自动化验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests scripts start_gui.py
.\.venv\Scripts\python.exe -m pip check
```

- 125 项单元测试通过；语法编译及依赖一致性检查通过。
- 覆盖连续追加、重复 C 编号零下载、原生库无清单接入、手工编辑保留、冲突、取消、并发修改、发布回滚和中断恢复。
- 新增非零旋转、正负 Z 偏移回归：先复现合并时模型元数据变化导致的保留校验失败，再验证新模型元数据与封装实例姿态一致后可连续追加。
- 故意修改模型元数据或 STEP 载荷，仍阻止发布并保留旧库；未放宽保留检查。
- 新增刷新门控、回执、超时、路径转义、权限错误、并发变化、GUI 无窗口回调和 CLI 退出码回归；测试不启动 AD，不操作桌面。
- 0.3.4 新增回归在修复前运行，刷新模块的 32 项测试出现 13 个失败（含子用例）；修复后全套 125 项通过。覆盖真实日志形态的 46/46 符号、0/46 封装，数量相等但名称不匹配或匹配标志缺失，封装未回读，以及临时文档所有权保护。
- 0.3.4 Windows 目录版 EXE 的离线 `doctor` 退出 0，确认版本为 0.3.4；`refresh-ad` 正确退出 1 并报告 `permission_required`、`verified=false`，未发出脚本请求、未创建 GUI、未请求提权。
- 从最终 EXE 内嵌的 Python 归档读取刷新模块并离线生成脚本，确认修复实际进入程序包：四个独立过程/函数、七处显式传参的过期/事务守卫，使用 PCB 库枚举器而非组件计数接口读取 PcbLib；符号和封装均验证数量及完整名称。检查未使用嵌套函数，唯一关闭调用只用于本次临时打开且未修改的目标文档。此检查不执行 AD 脚本。
- 本次未重新下载或生成元件，未改写正式总库及其权限。当前普通权限进程读取正式两份库文件收到 Windows 拒绝访问，故本版本未完成这两份库的原生回读或前后哈希验证；不能沿用上一版本对旧库快照的验证作为当前库的证据。

3D 单元测试的最小 STEP 夹具只验证嵌入与保留。底层库可能提示无法推断其包围盒，不代表真实器件几何验收。

## AD 缓存刷新验证与边界

- 0.3.2 用户实机报 `Can't access top level variable`，实际生成脚本第 12 行为嵌套 `Finish` 中的 `Report.Add(...)`。该对象是外层主过程的局部变量，与官方明确禁止的嵌套函数访问外层变量情形一致。
- 0.3.3 将回执和过期检查改为独立辅助函数，显式传入本次 `Report`，不使用全局可变状态。两项新增回归在修复前失败、修复后通过，检查无嵌套声明、仅主入口无参数，以及全部状态分支与调用都显式传参。
- 后续实机 0.3.3 回执已到达 `complete=1`、`status=stale`，符号数量随追加从 45 变为 46，封装数量始终为 0。说明该次脚本已通过原作用域报错位置，但不能据此断言面板已更新。
- 0.3.3 错将 `IIntegratedLibraryManager.GetComponentCount/GetComponentName` 用于独立 PcbLib；该接口枚举的是原理图或集成库组件，不能以此判定独立封装库为空。0.3.4 改用 `PCBServer.GetPCBLibraryByPath` 和 `IPCB_Library` 枚举器，读取每个 `IPCB_LibComponent.Name`。
- 对已打开的目标文档只复用、不关闭。未加载的目标 PcbLib 通过 `OpenDocumentShowOrHide('PCBLIB', ..., False)` 临时打开；仅在本次拥有该文档且仍未修改时关闭。未调用 `ShowDocument`，但 `AShowInTree=False` 不构成对所有 AD 版本中完全不可见或无工作区痕迹的保证，实际表现仍待确认。
- 警告现在区分实际回读为零与未完成回读，并给出实际/期望数量；数量或名称不匹配仍不能报告成功，不再笼统要求重装库或重新下载。
- 本机安装的 AD 26.9.1 命令注册文件包含 `IntegratedLibrary:RefreshInstalledLibraries`（`AllLibraries=True`）、`Altium.Edp.ComponentSearch.Plugin:ClearCache`、`ScriptingSystem:RunScriptFile`。
- 官方文档确认 `IServerDocument.Modified`、`SupportsReload`、`DoFileLoad`、PCB 库枚举器和文档查询/打开/关闭接口；实现只针对目标两份库，不保存或关闭原本打开的库或工程。
- 实际只读启动探针收到 Windows 740（需要提升权限）。正式模块及 CLI 能识别权限不一致，返回 `permission_required`、`verified=false`；未请求 UAC、未修改系统设置、未操作 GUI。
- 模拟 AD 回执的回归检查确认：无回执/错误请求 ID/数量不符/目标库变化/AD 会话变化均不能报告刷新成功；只刷新不会重新下载或生成库；目标库字节保持不变。
- **尚未验证**：在同权限 AD 会话中执行 0.3.4 生成脚本、新 PCB 枚举器的真实回读、临时文档行为、Components 面板即时显示效果。当前普通权限进程与管理员 AD 不匹配，未绕过权限门控；Windows 权限门控、生成脚本结构检查和单元测试不等于这些实际效果已通过。
- 成功回执的验证范围明确为 `ad_library_readback`：AD 库接口的条目回读，不等于对面板像素或交互显示的验证。
- 使用者应让 AD 和本工具以相同权限运行，再点击“刷新 AD 库”检查回执；推荐两者均为普通权限。脚本失败不会回滚已提交的库。

接口依据：[Altium 脚本运行](https://www.altium.com/documentation/altium-designer/scripting/running-scripts)、[文档重载与修改状态](https://www.altium.com/documentation/altium-dxp-developer/iserverdocument-interface)、[组件库管理器](https://www.altium.com/documentation/altium-dxp-developer/integrated-library-api?version=4.0)、[PCB 库与枚举器](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-system-interfaces-reference)、[文档所有权相关接口](https://www.altium.com/documentation/altium-dxp-developer/iclient-interface)、[DelphiScript 嵌套函数限制](https://www.altium.com/documentation/altium-designer/scripting/delphiscript/delphi-differences?version=23)。

## 0.3.1 阶段的原生库副本验证

使用 41 元件原生库的独立副本，连续执行接入、新增、重复提交、再新增：数量为 41 → 42 → 42 → 43。

- 原有符号及引脚记录、封装图元字节、模型 ID/元数据及解压后 STEP 载荷全部保持一致。
- 新测试件包含非零 3D 偏移和旋转；只在验证副本中使用本地已有 STEP。
- 重复请求不下载、不改写；最终原生回读验真通过。
- 正式库未被改写，验证期间模型网络请求为零。

复现脚本为 `scripts/validate_master_library.py`，要求提供使用者自己的源库和新的独立输出目录。仓库与 Release 不分发上述库、模型、缓存、个人路径或原始维护记录。

## 已知边界

- 未做本版本 Altium GUI 可视检查、真实断电测试或每种器件的工程签核；GUI 测试只覆盖无窗口回调。
- 投板前仍须按数据手册检查引脚、焊盘、Pin 1、尺寸、3D 高度和方向。
- 追加前应在 Altium 保存并关闭两份库。应用锁无法让其他软件配合互斥，也不能消除最后替换瞬间的全部外部写入竞争。
- 非标准模型元数据、未知字段或未被封装引用的模型，可能触发底层合并的保留校验失败；此时保守停止，不静默改写历史数据。
- 来源没有 STEP 时可保留符号/封装并报告缺失；已有 C 编号不会自动补模型。失败批次的 STEP 没有持久缓存，重试可能重新请求。
- 公共网页配套 JSON 端点没有稳定性承诺；本次验证未重跑在线搜索、下载或正式开放平台业务接口。
- 构建器可能提示可选制造导出模块缺少 shapely，本应用不使用该模块。Windows 发布包必须完整保留 EXE 与 `_internal`。
