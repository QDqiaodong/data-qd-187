# 样例来源

本仓全部样例与预期结果为作者自制合成数据，采用本仓 MIT 许可，不来自真实客户或实际作业。规则是 docs/contract.md 中明确的有界模型。

`--archive-build SOURCE --output ARCHIVE` 把旧版四字段单录次归档为自包含单文件，`--archive-query REQUEST --archive ARCHIVE --output RESULT` 按精确微秒窗口分页取回与窗口有正时长交集的声部。`fixtures/query.json` 是查询 `fixtures/input.json` 归档 `[0, 2500000)`、每页两条的示例请求；它只引用同一个合成样例，不引入真实数据。
