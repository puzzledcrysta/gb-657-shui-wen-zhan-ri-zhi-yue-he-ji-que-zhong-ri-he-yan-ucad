# 运行手册 hydrocalendar

## 1 启动

```bash
cd projects/gb-657
docker compose up -d --build
curl -sS http://localhost:18216/api/health
```

页面：<http://localhost:18216/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18216；SQLite 落在命名卷 `hydrocalendar-data` 的 `/data/hydrocalendar.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载五份文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18216/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18216/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18216/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。

## 5A 汛后留档与原始日值清理

汛后要按期清掉原始日值来件，但抽查仍要能解释某天为何采信或隔离。清理不是直接删表，
必须先走「留档 → 校验 → 清理」三步（导出页「留档与清理」面板，或 M8 接口）：

```bash
# 1. 留档：凭证写到 var/archives/<batch_id>/，逐文件登记 sha256 与整包指纹
curl -sS -X POST http://localhost:18216/api/batches/$BID/archive
# 2. 校验：读磁盘实物比对登记哈希，全部一致才放行
curl -sS http://localhost:18216/api/batches/$BID/archive/verify
# 3. 清理：校验通过后删 readings 原始日值（同事务，失败整体回滚）
curl -sS -X POST http://localhost:18216/api/batches/$BID/cleanup
```

留档目录每份批次包含六份文件：

| 文件 | 回答的问题 |
|---|---|
| `batch-receipt.csv` | 这批收了多少条，采信/估抄/隔离/空行各多少，差额是否为 0 |
| `daily-ledger.csv` | 每个原始行号的裁决：采信还是隔离、原因码、是否参与月量、区间号、归属月份 |
| `readings-evidence.csv` | 采信/估抄行的原值、折合值与行号（清理后读数导出回退到此文件） |
| `quarantine-evidence.csv` | 隔离行原值、原因码与说明 |
| `run-manifest.json` | 运行清单：输入 sha256、阶段计数、四账 |
| `checksums.json` | 上述五份文件各自 sha256 与整包指纹 |

清理后的对账方式（不依赖原始来件、也不依赖库内 readings 表）：

- 月报 `monthly.csv`、区间、隔离、运行清单照常从库内保留的月量/区间/隔离表导出；
- 读数证据与逐日台账改从留档目录取，**导出前先按登记 sha256 复核**，被改即报
  `E_ARCHIVE_CORRUPT`，不吐出失真数据；
- 抽查某一天：在 `daily-ledger.csv` 按「当地日期 + 表号」定位，看每条记录的
  「裁决 / 原因码 / 参与月量 / 说明」，同一天几条都没算进去时逐条给原因。

留档的写保护与重试约定：

- 凭证只写一次：重复留档返回既有凭证（`reused=true`），文件字节与 mtime 不变；
  目录已存在但无登记记录时报 `E_ARCHIVE_DIR_EXISTS`，拒绝覆盖；
- 已留档/已清理批次的强制重算与重试报 409 `E_BATCH_RETAINED`，凭证不可覆盖；
- 清理可安全重试：已清理再调只回显结果，不重复删除、不动凭证；
- 清理失败（校验不过、事务中断）不删任何日值，归档登记停在 `archived`，排除故障后重试。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HYDROCALENDAR_DB` | `/data/hydrocalendar.sqlite` | SQLite 路径 |
| `HYDROCALENDAR_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
