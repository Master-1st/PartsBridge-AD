# 元件库桥（PartsBridge AD）

0.3.1 修复：带 3D 高度偏移或旋转的新元件可正常追加；模型元数据与封装实例使用一致的姿态，完整性校验仍然开启。0.3.0 未发布的失败批次可以重试，无需清空旧库。

面向 Windows 和 Altium Designer 的本地桌面工具：以立创商城/LCSC 为元件检索与来源线索，先让用户确认 C 编号，再追加到原生 `SchLib` / `PcbLib` 总库。软件不读取浏览器 Cookie、不要求商城账号，也不会自动把模糊匹配结果写入工程。

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
4. 在 Altium 中打开两个库文件，并按数据手册做最终工程复核。

关键词/近似匹配只产生候选，不会自动选料。追加操作保留历史：已有 C 编号直接跳过，不下载数据，也不自动更新模型；只有新增且完整通过检查的元件才会进入总库。追加前请在 Altium 中保存并关闭 `LCSC.SchLib` 与 `LCSC.PcbLib`；应用自己的锁不能阻止 Altium 对已打开文件的写入。界面的查询和追加都在后台线程运行，可停止；停止、冲突、并发修改或整批失败都不会覆盖上一次已发布的库。

`_Lib` 目录只保留 `LCSC.SchLib` 和 `LCSC.PcbLib`，不写原理图。`manifest.json`、`last-run.json`、锁文件和每次发布前的备份位于 `%LOCALAPPDATA%\PartsBridge-AD` 下的每库 `state_directory`（运行结果会显示实际绝对路径）。
上次明确选择的总库目录偏好保存在 `%LOCALAPPDATA%\PartsBridge-AD`。
`manifest.json` 是库外索引，不是原生库本体；完整且可解析的旧库即使没有旧 manifest，也可以直接接入并从原生库重建索引，不要求重新下载元件。

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
```

`prepare` 未指定 `--output` 时使用界面上次提交追加的目录；0.3.1 尚未选择过时的默认值为 `G:\dontdel\AD\_Lib`。首次使用请主动选择自己的目录，没有 G 盘时必须改选；CLI 可用 `--output` 指定。CLI 显式 `--output` 只影响该次命令，不改写界面偏好。

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
- 候选中的价格来自 LCSC Global，明确标为 `USD` / `price_source=global`，不会按汇率伪装成人民币成交价；
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
