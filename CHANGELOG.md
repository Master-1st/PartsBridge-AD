# PartsBridge AD v0.3.1

元件库桥：面向立创/LCSC 元件检索的 Windows 本地工具，将确认后的 C 编号追加到长期维护的 Altium 原生库。

## 本版本

- 搜索和批量查询候选；由使用者核对型号后加入队列。
- 输出一套 `LCSC.SchLib` / `LCSC.PcbLib`，保留历史符号、引脚、封装及内嵌 STEP；不写入原理图。
- 已有 C 编号提前跳过；新增元件在暂存目录验证，发布前备份，支持受保护的中断恢复。
- 修复非零 3D 高度偏移或旋转导致的 `native item preservation failed`；完整性校验仍然开启。
- Windows 发布包包含依赖许可证和来源说明；不包含个人元件库、BOM、缓存、账号凭据或模型文件。

## 下载与使用

- `PartsBridge-AD-v0.3.1-Windows-x64.zip`：完整解压后运行 `PartsBridge-AD.exe`；不要只复制 EXE。
- `PartsBridge-AD-v0.3.1-source.zip`：应用源码、测试和构建配置。
- `SHA256SUMS.txt`：下载文件的 SHA-256 校验和。

首次启动请选择自己电脑上存在且可写的总库目录。0.3.1 在没有已保存偏好时的默认目录使用 G 盘；没有 G 盘时必须改选。

追加前在 Altium 中保存并关闭两份库。失败批次的 STEP 没有持久缓存，重试可能重新请求；已有元件不会自动更新或补模型。

## 验证与边界

82 项单元测试通过；原生库副本连续追加和重复跳过验证通过，原有符号、封装和 STEP 保持一致。Windows 包通过离线自检及原生库验真。

这些结果不等于 Altium GUI 可视检查或工程签核；投板前必须对照数据手册复核。公开 JSON 数据源可能限流或变化，库存/价格不保证等同中国站实时成交信息。详细边界见 `VALIDATION_REPORT.md` 和 `README.md`。

本项目采用 AGPL-3.0-or-later；第三方组件遵循各自许可证。本项目与立创、嘉立创、EasyEDA、Altium 无隶属或背书关系。
