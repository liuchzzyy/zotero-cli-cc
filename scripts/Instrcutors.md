## Clean-up all metadata

### 推荐给代理的直接提示词
```text
使用 skill zotero-cli-agents。
先读取 Zotero 条目 metadata，使用 `uv run zot --json --detail full summarize-all --exclude-tag update/metadata --limit 5000 > metadata-export.json` 导出未处理条目。
只清洗这些字段的格式问题：title、abstractNote、publicationTitle、journalAbbreviation、language、publisher。
清洗目标：去掉 HTML 标签，修复异常空格、断裂换行、特殊符号粘连；保持原意，不改事实内容。
边界处理：化学式、化学计量数和电荷不要插入空格，例如 CO2、H2O、MnO2、Zn2+、LiFePO4、Ni3S2；不要把小数改成 `1. 0`；不要把 `single- versus`、`regio- and` 这类并列短语合并成一个词。
不要修改 DOI、url、date、pages、ISSN、extra.extra、creators、tags、notes。
只输出实际发生变更的条目，生成 cleaned-metadata.jsonl。
先执行 `uv run zot --json update --from-jsonl cleaned-metadata.jsonl --dry-run > logs\metadata-cleanup-dry-run-YYYYMMDD-HHMM.json`，不要正式写入，等我确认。
我确认后，按 25-100 条切分 cleaned-metadata.jsonl 分批正式写入，避免长批次超时或 API 断连；如果条目很少，可以只生成一个批次，但仍按批次记录。
每批先在终端实时打印进度，例如 `[batch 2/8] applying 75 items -> logs\metadata-cleanup-apply-batch-2-YYYYMMDD-HHMM.json`，再执行 `uv run zot --json update --from-jsonl cleaned-metadata-batch-N.jsonl --add-tag update/metadata > logs\metadata-cleanup-apply-batch-N-YYYYMMDD-HHMM.json`；如果确实只跑一个完整文件，可用 `uv run zot --json update --from-jsonl cleaned-metadata.jsonl --add-tag update/metadata > logs\metadata-cleanup-apply-YYYYMMDD-HHMM.json`。
不要静默等待长批次；每批结束后立即报告成功数、失败数、剩余批次数和日志路径。
如果某批超时或断连，不要盲目重跑全量；先用 Web API 复核哪些条目已经同时完成字段更新和 `update/metadata` tag，再只续跑未完成条目。
全部批次完成后，复核 cleaned-metadata.jsonl 中所有 key 都已完成字段更新并带有 `update/metadata` tag。
复核无误后，清理根目录中间文件：metadata-export.json、cleaned-metadata.jsonl、cleaned-metadata-batch-*.jsonl、cleaned-metadata-remaining*.jsonl；保留 logs 作为记录。
```

## Daily RSS DOI Import

### 推荐给代理的直接提示词
```text
不要手动拆开执行 RSS DOI 导入流程。
直接在 E:\Desktop\CodingDaily\zotero-cli-agents 下调用 scripts\run-rss-daily-doi-import.ps1 做日常导入。
默认读取 rss-cli-agent\storage\daily\当天.selected.json。
如果需要，使用 -Date 指定日期，使用 -ProgressIntervalSeconds 调整进度刷新频率。
导入完成后检查根目录的 rss_failed_dois_YYYY-MM-DD.txt；如果没有失败且脚本成功结束，tmp 应该被自动删除。
```

## Remove Newer DOI Duplicates

### 推荐给代理的直接提示词
```text
不要用 title 模糊匹配做去重。
直接在 E:\Desktop\CodingDaily\zotero-cli-agents 下调用 scripts\remove-newer-doi-duplicates.ps1。
规则固定为：只按 DOI 精确判断；同 DOI 时保留 date_added 更早的旧条目，删除 date_added 更晚的新条目。
执行时必须给出实时进度：查询 DOI 重复项、构建 keep/delete 计划、每个重复组的 keep/delete 判断；正式删除时还要报告批次编号、已删除数、失败数、总体百分比。
先执行默认 dry-run 看 keep/delete 计划；我确认后，再加 -Apply 正式删除。
```
