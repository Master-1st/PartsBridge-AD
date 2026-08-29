# PartsBridge AD 0.3.8 验证说明

验证日期：2026-08-29。环境：Windows 11 x64、Python 3.12.10、PyInstaller 6.22.2。

## 自动化验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests scripts start_gui.py
.\.venv\Scripts\python.exe -m pip check
```

- 142 项单元测试通过；语法编译及依赖一致性检查通过。
- 覆盖连续追加、重复 C 编号零下载、原生库无清单接入、手工编辑保留、冲突、取消、并发修改、发布回滚和中断恢复。
- 新增非零旋转、正负 Z 偏移回归：先复现合并时模型元数据变化导致的保留校验失败，再验证新模型元数据与封装实例姿态一致后可连续追加。
- 故意修改模型元数据或 STEP 载荷，仍阻止发布并保留旧库；未放宽保留检查。
- 新增刷新门控、回执、超时、路径转义、权限错误、并发变化、GUI 无窗口回调和 CLI 退出码回归；测试不启动 AD，不操作桌面。
- 0.3.8 新增三项 PcbDoc 原生流回归：成功路径仅允许 `Pads6/Data` 变化；模型数量头与实际记录不一致时停止；焊盘流长度变化时停止并删除未通过验证的输出。输出再次解析后须与修复计划的全部焊盘字段一致，模型表和 3D Body 引用必须完整。
- 使用一份不进入仓库的真实板做故障—恢复验收：226 个组件、787 个焊盘、230 个 3D Body。恢复时从原生备份保留全部非焊盘流，只带回 198 个焊盘的 X 坐标修正；重新打开后由 AD 26.9.1 成功原生保存。保存后的文件含 164 个原生流，模型表数量头与实际记录均为 54/54，`ModelsNoEmbed` 为 0/0，悬空模型引用为 0。该证据只证明本次 PcbDoc 恢复与保存，不等于元件库追加和 Components 面板验收。
- 0.3.7 新增 STEP 最低点归一化、插件针脚深度保留和解析失败保守回退回归。公式为 `模型 Z 偏移 = 立创目标最低高度 - 旋转后 STEP 原始最低高度`，不会把来源 Z 再次叠加到模型已有坐标。
- 真实 STEP 隔离验证覆盖 `C77069`、`C882133`、`C2992422`、`C918121`、`C41355418`、`C918124`、`C918129`：0805 最终底面为 0；插件件保留 -3.5 mm/-4.0 mm 针脚深度；特殊电源模块自动得到 +6.1 mm 模型偏移。7 个封装写入临时 PcbLib 后原生回读的底面、顶面和模型 Z 偏移全部匹配，未改写正式总库。
- 0.3.8 Windows 目录版 EXE 的离线 `doctor --json` 退出 0；源码环境报告版本 0.3.8，Python、Tk、EasyEDA 转换器、Altium 原生库写入器和签名向量检查通过。打包后的 EXE 在线搜索 `TD331SCANH --in-stock` 退出 0；对内嵌 Python 归档的回读确认中国站接口、`price_source=china` 标记和 STEP Z 归一化函数均已进入发布包。EXE 文件/产品版本为 `0.3.8.0`，`CompanyName=foke`，SHA-256 为 `33259CFCA3F0EF37026CBD4895889E7AF4413CBDD66476F41972CE13BAD070E7`；Authenticode 状态为 `NotSigned`。
- 0.3.6 新增三项中国站数据回归：制造商料号优先返回中国站库存和人民币价格，C 编号可在全球详情缺失时由中国站补齐，勾选“仅有库存”后不会用全球结果替换中国站已确认的缺货记录。
- 当前在线请求实测 `TD331SCANH` 与 `C7527764` 均精确返回 `C7527764`、库存 2、人民币首价 32.53、封装 SMD-9。随后在自动清理的隔离临时目录实际下载公开 CAD 数据并生成 `C7527764_TD331SCANH` / `C7527764_SMD-9`，STEP UUID `8fbce6a133804d1298a652b290620c33` 已嵌入；SchLib/PcbLib 哈希、原生库存、图元、模型及符号—封装链接验真通过。未读写正式总库。
- 0.3.6 Windows 目录版 EXE 的离线 `doctor` 退出 0 并报告版本 0.3.6；由打包后的 EXE 在线搜索 `TD331SCANH --in-stock`，精确返回 `C7527764`、库存 2、人民币首价 32.53、封装 SMD-9 和 `price_source=china`，证明中国站修复已进入发布包。
- 0.3.6 EXE 文件属性实测为 `CompanyName=foke`、`ProductName=PartsBridge AD`、文件/产品版本 `0.3.6.0`；`Get-AuthenticodeSignature` 实测为 `NotSigned` 且无签名证书。
- 0.3.5 新增六项写库前权限与库对诊断回归：非 Windows、AD 未运行、唯一同权限 AD 允许；权限不一致、多 AD 会话、检测异常在 `LibraryStore`、目录创建和器件获取前停止；诊断仅在发布清单哈希证明变化时指出被改写文件，且只显示实际包含完整库对的备份。
- 使用当前在线数据在隔离临时目录复现日志顺序：C32713268 首次发布，随后 C192062/AD5422BREZ-REEL 连续追加并嵌入 STEP。再次读取获得 2 个符号、2 个封装、2 个模型且验真通过；另一次继续追加第三个元件也通过。此结果排除当前数据/转换器稳定复现，但不解释对方电脑上发布后发生的外部改写。
- 0.3.5 Windows 目录版 EXE 的离线 `doctor` 退出 0 并报告版本 0.3.5。真实权限不一致环境下运行打包后的 `prepare`，在输出目录创建、网络获取和库写入前退出 1；输出目录保持不存在，未派发 AD 脚本或请求提权。
- `pyproject.toml`、运行时模块和新维护清单将维护/发布者记录为 `foke`；这不构成代码签名，也不能改写 GitHub 服务端显示的登录账号。
- 重建后的 Windows EXE 文件属性实测为 `CompanyName=foke`、`ProductName=PartsBridge AD`、文件/产品版本 `0.3.5.0`；`Get-AuthenticodeSignature` 实测为 `NotSigned` 且无签名证书，明确区分产品元数据与 Windows 的受信任签名发布者。
- 0.3.4 新增回归在修复前运行，刷新模块的 32 项测试出现 13 个失败（含子用例）；修复后全套 125 项通过。覆盖真实日志形态的 46/46 符号、0/46 封装，数量相等但名称不匹配或匹配标志缺失，封装未回读，以及临时文档所有权保护。
- 0.3.4 Windows 目录版 EXE 的离线 `doctor` 退出 0，确认版本为 0.3.4；`refresh-ad` 正确退出 1 并报告 `permission_required`、`verified=false`，未发出脚本请求、未创建 GUI、未请求提权。
- 从最终 EXE 内嵌的 Python 归档读取刷新模块并离线生成脚本，确认修复实际进入程序包：四个独立过程/函数、七处显式传参的过期/事务守卫，使用 PCB 库枚举器而非组件计数接口读取 PcbLib；符号和封装均验证数量及完整名称。检查未使用嵌套函数，唯一关闭调用只用于本次临时打开且未修改的目标文档。此检查不执行 AD 脚本。
- 0.3.4 阶段未重新下载或生成元件，也未改写正式总库及其权限。当时普通权限进程读取正式两份库文件收到 Windows 拒绝访问，因此不能把旧库快照验证当作其他电脑当前库的证据。0.3.5 的在线复现只使用自动清理的隔离临时目录，未接触正式总库。

最小 STEP 夹具用于验证嵌入、保留和失败回退；0.3.7 另使用上述 7 个真实模型验证几何边界与原生回读。该验证仍不等于 Altium GUI 可视验收或每个来源模型的工程签核。

## AD 缓存刷新验证与边界

- 0.3.2 用户实机报 `Can't access top level variable`，实际生成脚本第 12 行为嵌套 `Finish` 中的 `Report.Add(...)`。该对象是外层主过程的局部变量，与官方明确禁止的嵌套函数访问外层变量情形一致。
- 0.3.3 将回执和过期检查改为独立辅助函数，显式传入本次 `Report`，不使用全局可变状态。两项新增回归在修复前失败、修复后通过，检查无嵌套声明、仅主入口无参数，以及全部状态分支与调用都显式传参。
- 后续实机 0.3.3 回执已到达 `complete=1`、`status=stale`，符号数量随追加从 45 变为 46，封装数量始终为 0。说明该次脚本已通过原作用域报错位置，但不能据此断言面板已更新。
- 0.3.3 错将 `IIntegratedLibraryManager.GetComponentCount/GetComponentName` 用于独立 PcbLib；该接口枚举的是原理图或集成库组件，不能以此判定独立封装库为空。0.3.4 改用 `PCBServer.GetPCBLibraryByPath` 和 `IPCB_Library` 枚举器，读取每个 `IPCB_LibComponent.Name`。
- 对已打开的目标文档只复用、不关闭。未加载的目标 PcbLib 通过 `OpenDocumentShowOrHide('PCBLIB', ..., False)` 临时打开；仅在本次拥有该文档且仍未修改时关闭。未调用 `ShowDocument`，但 `AShowInTree=False` 不构成对所有 AD 版本中完全不可见或无工作区痕迹的保证，实际表现仍待确认。
- 警告现在区分实际回读为零与未完成回读，并给出实际/期望数量；数量或名称不匹配仍不能报告成功，不再笼统要求重装库或重新下载。
- 0.3.5 将同一 Windows 进程权限检查前移至每次追加：AD 未运行允许离线生成；AD 正在运行时，唯一会话且权限一致才继续。该检查不启动、关闭或注入 AD，不请求 UAC；即使关闭自动刷新也不能绕过写库门控。
- 现有库原生回读失败仍保守停止。仅当上次成功 manifest 的输出元数据与当前文件 SHA-256/大小不同，才报告发布后变化的具体文件；完整配对备份存在时只提供人工核对路径，不自动恢复。
- 本机安装的 AD 26.9.1 命令注册文件包含 `IntegratedLibrary:RefreshInstalledLibraries`（`AllLibraries=True`）、`Altium.Edp.ComponentSearch.Plugin:ClearCache`、`ScriptingSystem:RunScriptFile`。
- 官方文档确认 `IServerDocument.Modified`、`SupportsReload`、`DoFileLoad`、PCB 库枚举器和文档查询/打开/关闭接口；实现只针对目标两份库，不保存或关闭原本打开的库或工程。
- 实际只读启动探针收到 Windows 740（需要提升权限）。正式模块及 CLI 能识别权限不一致，返回 `permission_required`、`verified=false`；未请求 UAC、未修改系统设置、未操作 GUI。
- 模拟 AD 回执的回归检查确认：无回执/错误请求 ID/数量不符/目标库变化/AD 会话变化均不能报告刷新成功；只刷新不会重新下载或生成库；目标库字节保持不变。
- **尚未验证**：在同权限 AD 会话中执行 0.3.8 实际追加、生成脚本、新 PCB 枚举器回读、临时文档行为、Components 面板即时显示效果。当前普通权限进程与管理员 AD 不匹配，未绕过权限门控；Windows 权限门控、生成脚本结构检查和单元测试不等于这些实际效果已通过。PcbDoc 恢复后的原生保存成功不能替代上述库工作流验收。
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

- 未做本版本元件库追加与 Components 面板的 Altium GUI 可视检查、真实断电测试或每种器件的工程签核；只完成了 PcbDoc 恢复后的重新打开与原生保存，GUI 自动测试仍只覆盖无窗口回调。
- 投板前仍须按数据手册检查引脚、焊盘、Pin 1、尺寸、3D 高度和方向。
- 追加前应在 Altium 保存并关闭两份库。应用锁无法让其他软件配合互斥，也不能消除最后替换瞬间的全部外部写入竞争。
- 非标准模型元数据、未知字段或未被封装引用的模型，可能触发底层合并的保留校验失败；此时保守停止，不静默改写历史数据。
- 来源没有 STEP 时可保留符号/封装并报告缺失；已有 C 编号不会自动补模型。失败批次的 STEP 没有持久缓存，重试可能重新请求。
- 公开网页配套 JSON 端点没有稳定性承诺；0.3.6 已重跑中国站搜索与公开 CAD 下载，但未使用或验证需审批密钥的正式开放平台业务接口。
- 构建器可能提示可选制造导出模块缺少 shapely，本应用不使用该模块。Windows 发布包必须完整保留 EXE 与 `_internal`。
