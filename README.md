# 元件库桥（PartsBridge AD）

0.3.8 安全修复：源码中的 PCB 焊盘恢复工具改为只替换等长的 `Pads6/Data` 原生流，不再重写整份 PcbDoc。模型表数量、3D Body 引用、原生流清单和非目标流哈希均须通过检查；任何失配都会停止并删除未通过验证的输出。真实故障板已在 AD 26.9.1 中完成恢复后重新打开和原生保存，个人板文件、库和模型不会进入仓库或发布包。

0.3.7 修复 3D 模型约一半埋入 PCB：工具读取旋转后 STEP 的真实最低点，再映射到立创定义的目标最低高度。贴片件底面自动贴板，插件件继续保留针脚穿板深度，不再把立创 Z 坐标重复叠加到 STEP 上。STEP 边界无法解析时保守沿用来源偏移并记录警告。

0.3.6 中国站搜索更新：搜索和 C 编号详情优先读取立创 EDA 中国站公开数据，中国站无结果或请求失败时才回退原有全球公开接口，解决同一型号在中国商城有货、海外数据分区却没有记录的问题。当前在线实测 `TD331SCANH` / `C7527764` 可返回国内库存、人民币价格、SMD-9 封装和中国商城商品页，并可生成带 STEP 3D 的 Altium 原生库。

**维护与发布：foke**

Windows EXE 文件属性会显示公司/发布名称 `foke`。这属于产品元数据，不是 Authenticode 数字签名；未签名程序在 SmartScreen 或 UAC 中仍可能显示“未知发布者”。只有由受信任证书签名才能成为 Windows 所称的“已验证发布者”。

面向 Windows 和 Altium Designer 的本地桌面工具：以立创商城中国站/LCSC 为元件检索与来源线索，先让用户确认 C 编号，再追加到原生 `SchLib` / `PcbLib` 总库。软件不读取浏览器 Cookie、不要求商城账号，也不会自动把模糊匹配结果写入工程。

> 独立第三方互操作工具，与嘉立创、立创商城、EasyEDA 或 Altium 无隶属、授权或背书关系；相关名称和商标归各自权利人所有。

## 最快开始

从 [GitHub Releases](https://github.com/Master-1st/PartsBridge-AD/releases) 下载 Windows x64 ZIP；同一发布页提供源码和 SHA-256 校验和。

Windows 发布版无需安装 Python。完整解压后，打开目录中的：

```text
PartsBridge-AD.exe
```

不要只复制单个 EXE；它需要同目录下的 `_internal` 文件夹。第一次运行若被 Windows SmartScreen 提示，这是因为当前个人构建未购买代码签名证书，请先核对发布包 SHA-256，再选择是否运行。

如需从源代码安装，运行环境固定为 Python 3.12（当前 `altium-monkey` 版本不支持 Python 3.14）：

```powershell
cd <解压后的源代码目录>
.\install.ps1
.\start-gui.ps1
```

桌面界面的标准流程是：

1. 输入 C 编号、制造商料号（MPN）或关键词并搜索；
2. 核对品牌、MPN、封装、数据手册和中国站商品页，手动加入生成队列；
3. 选择自己电脑上存在且可写的长期总库目录，点击“追加到总库”；
4. 首次使用时在 Altium 的 Components 面板中安装这两份文件库；后续追加由工具请求刷新，并按数据手册做最终工程复核。

关键词/近似匹配只产生候选，不会自动选料。追加操作保留历史：已有 C 编号直接跳过，不下载数据，也不自动更新模型；只有新增且完整通过检查的元件才会进入总库。追加前请在 Altium 中保存并关闭 `LCSC.SchLib` 与 `LCSC.PcbLib`；应用自己的锁不能阻止 Altium 对已打开文件的写入。界面的查询和追加都在后台线程运行，可停止；停止、冲突、并发修改或整批失败都不会覆盖上一次已发布的库。

`_Lib` 目录只保留 `LCSC.SchLib` 和 `LCSC.PcbLib`，不写原理图。`manifest.json`、`last-run.json`、锁文件和每次发布前的备份位于 `%LOCALAPPDATA%\PartsBridge-AD` 下的每库 `state_directory`（运行结果会显示实际绝对路径）。
上次明确选择的总库目录偏好保存在 `%LOCALAPPDATA%\PartsBridge-AD`。
`manifest.json` 是库外索引，不是原生库本体；完整且可解析的旧库即使没有旧 manifest，也可以直接接入并从原生库重建索引，不要求重新下载元件。

## 更新后刷新 AD 库

界面默认勾选“追加后刷新 AD 库”。真正发布了新增元件后，工具在库文件事务及锁结束后，通过 AD 自带脚本接口请求：重载这两份已打开且没有未保存修改的库、刷新已安装库缓存、清除 Components 面板缓存。不会保存或关闭原本打开的库/工程、卸载/重新安装库，也不会删除 AD 配置目录。重复料号全部跳过、整批失败或取消时，不自动刷新。

原理图符号由组件库接口回读，PCB 封装由 PCB library iterator 枚举，不能把这两类库混用一个组件计数接口。目标 PcbLib 若尚未加载，工具会通过 AD 接口临时打开它用于回读，不调用 `ShowDocument`；仅当该文档由本次打开且回读后仍无修改时才关闭。原本存在的文档始终保留；若检测到未保存修改则停止后续操作，不保存或强制关闭。AD 对临时加载的具体界面呈现仍需实机确认。

“刷新 AD 库”按钮只重试刷新，不查料、不下载、不重新生成库。刷新失败会与“库已追加”分开报告；不会回滚已生成的库，也不会把部分成功的追加误报成整批失败。脚本和回执保存在 `%LOCALAPPDATA%\PartsBridge-AD\ad-refresh`，不放进总库目录。未收到回执或条目不匹配时不会显示“刷新成功”。超时请求会失效，不会在之后的会话中无限等待执行。

运行条件：

- 仅支持 Windows；当前会话中需要有且只有一个正在运行的 Altium Designer。AD 没运行时只跳过，不主动启动。
- AD 与元件库桥需要相同运行权限。推荐都以普通权限运行；如果 AD 已以管理员身份运行，需用相同权限启动元件库桥。程序不会主动申请提权或更改系统设置。
- 0.3.5 起在器件下载和总库目录创建前执行上述权限检查；多 AD 会话、权限不一致或无法可靠判断时均停止。AD 未运行不影响离线建库。
- 这两份库存在未保存编辑时停止刷新，保护编辑内容；其他原理图/PCB 工程不会被保存、关闭或重载。
- AD 弹窗或交互操作可能阻塞脚本；结束操作后可点击“刷新 AD 库”。

如果 0.3.2 弹出 `Can't access top level variable` / `Continue execution?`，先选择 No 停止旧脚本。如果 0.3.3 报数量不匹配、封装数被误读为 0，关闭旧版元件库桥，完整解压并运行 0.3.4，然后仅点击“刷新 AD 库”；不必重新追加或下载。

本机已核实 AD 26.9.1 的命令注册和权限保护路径。0.3.5 使用当前在线数据按“C32713268 → C192062 → 再追加”的顺序完成隔离复现，包含 STEP 嵌入的符号、封装和模型再次读取均通过；因此真实日志中的失配不能归因于 C192062 本身。0.3.6 另以中国站在线数据完成 `TD331SCANH` / `C7527764` 精确搜索及隔离建库，SchLib、PcbLib、内嵌 STEP、原生回读和链接验证均通过。0.3.7 使用 7 个真实贴片/插件 STEP 验证最低点归一化及原生 PcbLib 回读。0.3.8 另完成真实 PcbDoc 恢复、重新打开及 AD 原生保存，证明模型表失配已消除；该验证不包含个人设计数据。若上次发布清单与当前文件哈希不同，新版会报告发生变化的文件和可核对的完整配对备份。当前自动验证进程仍与管理员 AD 权限不同；尚未完成 0.3.8 的同权限 AD 实际追加、脚本回读、临时文档和面板可见性验收。详见 `VALIDATION_REPORT.md`。接口依据：[Altium 脚本运行](https://www.altium.com/documentation/altium-designer/scripting/running-scripts)、[文档重载](https://www.altium.com/documentation/altium-dxp-developer/iserverdocument-interface)、[PCB 库枚举](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-system-interfaces-reference)、[文档打开与关闭](https://www.altium.com/documentation/altium-dxp-developer/iclient-interface)、[DelphiScript 作用域限制](https://www.altium.com/documentation/altium-designer/scripting/delphiscript/delphi-differences?version=23)。

## 批量 CSV

待查询 CSV 接受首列，或字段名 `query`、`mpn`、`manufacturer_part_number`。已确认元件 CSV 接受首列，或字段名 `lcsc`、`product_code`、`code`。支持 UTF-8 BOM 和常见 Windows 中文 CSV（GB18030）。

命令行示例：

```powershell
# 单个查询
.\.venv\Scripts\lcsc-altium.exe search STM32F103C8T6 --limit 10 --in-stock

# 批量查询并导出候选
.\.venv\Scripts\lcsc-altium.exe resolve `
  --input-csv examples\queries.csv `
  --output output\candidates.csv `
  --limit 5 --in-stock

# 只对确认后的 C 编号追加到默认长期总库
.\.venv\Scripts\lcsc-altium.exe prepare C25804 C8734
.\.venv\Scripts\lcsc-altium.exe prepare `
  --input-csv examples\components.csv

# 如需本次指定其他总库目录
.\.venv\Scripts\lcsc-altium.exe prepare C25804 --output D:\PartsBridge\_Lib

# 默认请求下载并嵌入 STEP；可显式关闭
.\.venv\Scripts\lcsc-altium.exe prepare C25804 --no-with-3d

# 单独刷新 AD，不下载、不重新生成
.\.venv\Scripts\lcsc-altium.exe refresh-ad --output D:\PartsBridge\_Lib --json

# 本次追加不请求刷新 AD
.\.venv\Scripts\lcsc-altium.exe prepare C25804 --no-refresh-ad
```

`prepare` 未指定 `--output` 时使用界面上次提交追加的目录；尚未选择过时的默认值为 `G:\dontdel\AD\_Lib`。首次使用请主动选择自己的目录，没有 G 盘时必须改选；CLI 可用 `--output` 指定。CLI 显式 `--output` 只影响该次命令，不改写界面偏好。

## 自检和总库验真

```powershell
.\.venv\Scripts\lcsc-altium.exe doctor --json
.\.venv\Scripts\lcsc-altium.exe verify --json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`prepare` 和 `verify` 的 `--output` 可省略并使用默认总库目录；`resolve` 的候选 CSV 输出仍必须显式指定 `--output`。`verify` 会核对：

- 清单版本与发布状态及库外维护文件；
- `SchLib` / `PcbLib` 的大小和 SHA-256；
- 原生库能否重新解析；
- 符号、封装、引脚、焊盘、图元、多单元 part 和符号—封装链接是否与清单一致；
- 自定义焊盘是否在原生文件回读后仍保留自定义轮廓。

每个新增元件先在隔离的内存库中完整生成并检查，成功后才进入暂存库；缺少或损坏任一库文件、维护文件冲突或检测到并发修改时，追加会安全中止并保留旧库。同名冲突的元件会被拒绝，同批其他通过检查的新元件仍可追加，并报告部分失败。若上次保存中断，仅当目标文件仍匹配本次事务记录的已知 before/after SHA-256 时才会自动恢复；发现未知外部修改则停止，不覆盖外部修改。新增、跳过、失败、总量、状态、锁和备份信息写入库外 `state_directory`；已有元件不会被更新。

备份位于 `state_directory\backups\<run_id>`，不自动删除。目录内通常含发布前的两份库及当时的索引；首次从空目录新建没有旧库可备份。整个总库的可移植本体是两份原生文件，已有符号中的绝对路径链接不会被自动改写；若人工搬迁目录，仍应检查这些链接。

默认勾选 3D 嵌入请求；来源没有 3D 资源时会记录警告，但这不等于模型完整或可制造，工程使用前仍须按数据手册复核封装、焊盘、Pin 1、3D 高度和方向。

## 当前转换范围

符号已覆盖：

- 引脚、矩形、圆、椭圆；
- 折线、多边形、直线/三次贝塞尔/二次贝塞尔路径、文字；
- EasyEDA 多单元符号到 Altium multi-part symbol。

封装已覆盖：

- 圆形、矩形、椭圆/圆角矩形和自定义多边形 SMD 焊盘；
- 圆孔、槽孔、独立非金属化安装孔、via；
- 走线、矩形、圆弧、圆、文字和可安全映射的区域；
- 可选嵌入 STEP 模型。

目前会明确拒绝而不是猜测的情况包括：椭圆/旋转的复杂符号圆弧、相对坐标或未知命令的符号路径、自定义通孔焊盘，以及无法解析的复杂区域。单个元件失败不会中断其他元件。

公共 EasyEDA 元件端点会进行按主机节流；成功响应在 `%LOCALAPPDATA%\PartsBridge-AD\component-cache` 原子缓存 7 天。缓存只用于相对静态的 CAD 元件数据，不缓存库存和价格。公共端点若返回 403，软件会如实记录失败；不要高频重试，稍后重跑时已成功项会命中缓存。

## 数据与证据边界

- 中国站商品链接来自 `item.szlcsc.com`；
- 中国站候选使用中国站返回的人民币阶梯首价并标为 `CNY` / `price_source=china`；只有中国站无结果或请求失败时使用全球回退结果的 `USD` / `price_source=global`，不会自行换算或伪装成交价；
- 库清单记录数据 URL、抓取时间/缓存状态、源 JSON SHA-256、输出文件 SHA-256 和转换警告；
- `manual_review_required` 始终为 `true`。

软件验证文件结构和转换一致性，但不能替代工程师对以下内容的签核：

- 数据手册中的引脚号、名称、电气类型与多单元分配；
- 封装尺寸、焊盘、阻焊/钢网开窗、安装孔和 Pin 1 方向；
- 3D 模型高度、偏移和旋转；
- 中国站实时库存、价格、生命周期和可采购性。

## 嘉立创正式开放平台

正式开放平台适配层已按官方签名规范实现，并通过官方 HMAC-SHA256 示例向量测试。凭据只从以下环境变量读取，不写入配置、日志、清单或压缩包：

```text
JLC_OPEN_APP_ID
JLC_OPEN_ACCESS_KEY
JLC_OPEN_SECRET_KEY
```

此版本仅包含签名与配置检查，不包含获批业务 API 的元件查询实现；公开数据模式不依赖上述凭据。正式业务接入需要使用者获得相应权限、配置官方 IP 白名单、依据获批文档实现业务方法并完成在线验收。不要把密钥提交到 GitHub。

## 开发与构建

使用 Python 3.12 安装项目后，运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m pip install PyInstaller==6.22.2
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm PartsBridge-AD.spec
```

构建结果位于 `dist\PartsBridge-AD`。发布时保留第三方许可证、源码获取地址和 `_internal` 目录；不要包含元件缓存、个人 CAD 库或本地维护清单。验证范围见 [VALIDATION_REPORT.md](VALIDATION_REPORT.md)。

## 接口和许可证

免登录模式使用 LCSC/EasyEDA 当前公开 JSON 数据，不抓取 HTML、不读取 Cookie。这些网页配套端点不是承诺长期稳定的正式开放 API，可能改变、限流或停止；网络访问集中在 `client.py`，变化会显式失败并可定位修复。

本项目使用 `easyeda2kicad 1.0.1` 和 `altium-monkey 2026.8.21`，两者均为 AGPL-3.0 系列许可证。本项目本身按 AGPL-3.0-or-later 提供；分发或部署网络服务前，请履行相应源代码义务。详见 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`。
