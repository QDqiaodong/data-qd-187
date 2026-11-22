# data-qd-187 · 踏板演奏事件与声部时长

把按键、抬键和通道踏板事件编译为独立声部；速度图精确映射节拍时间，按键释放与实际止音共同决定延音和峰值复音。

voices.py 维护声部与踏板因果，clock.py 精确积分速度图，service.py 联合输出时长证据，comp.py 把对齐到同组拍点的多份录次剪辑成一份可回放演奏。 完整字段、单位、身份与失败约定见 [数据契约](docs/contract.md)。样例是作者自制合成数据，许可见 [来源说明](fixtures/SOURCES.md)。

## 安装、测试与演示

Python >=3.11，本批实际使用本机 CPython3.12.14。运行与测试仅用标准库；构建工具精确锁定。每仓独立 .venv，公共包缓存可共享；不共享其他项目的site-packages。

下面命令已在本机已有uv、Python与公共缓存条件下离线执行通过。其他机器如未准备缓存，需先准备构建包；不保证首次安装可以离线完成。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/pedal-performance --source fixtures/input.json --output run/report-initial.json
.venv/bin/pedal-performance --source fixtures/input-score.json --output run/report-score.json
.venv/bin/pedal-performance --comp fixtures/input-comp.json --output run/report-comp.json
```

实际123项测试通过；CLI演示生成JSON，摘要为：

```json
{
  "peak_polyphony": 2,
  "pedal_extended_voices": 2,
  "unfinished_keys": 1,
  "voices": 3
}
```

带乐谱样例 `fixtures/input-score.json` 的摘要额外包含评估计数（共10项）：

```json
{
  "peak_polyphony": 2,
  "pedal_extended_voices": 2,
  "unfinished_keys": 2,
  "voices": 4,
  "matched_cues": 3,
  "missed_cues": 1,
  "extra_voices": 1,
  "onset_ok": 2,
  "duration_ok": 2,
  "duration_undetermined": 1
}
```

输出是原子新建；演示已生成过的同名路径不能重用，复跑请换一个新文件名。非法输入与文件写入错误退出2；业务诊断的表达方式见契约。已经实际核验重复JSON键拒绝、旧结果字节不变、已有符号链接不覆盖。

## 多录次剪辑

`--comp MANIFEST` 与 `--source` 二选一。清单把同一首曲子的多份录次对齐到同一组切点，并按段给出剪辑代价；编译器先发布每个切点上各录次的按键、踏板延音与踏板通道诊断，再在换录预算与精确播放时长区间内，依次按总代价、换录次数、take_id 序列字典序选出全局最优分段方案。换录次只在两侧都无活跃声部的切点发生，并按需补发踏板事件，不切断按键与延音，也不改变下一录次原有的止音行为。输出包含可直接作为 `--source` 回放的合成 `performance`、逐事件来源追溯与完整演奏报告；无解时仍发布诊断并标 `no_plan`，清单无效退出 2。字段与规则细节见契约。

演示清单 `fixtures/input-comp.json` 的摘要为：

```json
{
  "status": "ok",
  "switches": 1,
  "total_cost": 5,
  "total_duration_us": "1000000"
}
```

## 乐谱提示评估

可选 `score` 给出 cues；每个 cue 按 channel/pitch 与声部做保序一对一归因，报告起音偏差/状态、按键时值偏差/状态、漏奏与多奏。归因依次最大化匹配数、最小化起音绝对偏差总和、最小化可观测按键时值绝对偏差总和，再取配对序列字典序最小者。两套 ppq/速度图各自积分，比较全程用整数缩放后的精确值。未释放按键的时值标为不可判定，不以踏板延音或 end_tick 强停冒充释放。字段与边界见契约。

## 录制顺序核验

录音器偶尔会把同一 tick 上相邻两条消息写反并在导出时标记这对消息，还能独立记录某些拍点上各通道仍按着的键数（`held`）与已松键但仍在发声的声部数（`sustained`）。`--capture-audit MANIFEST` 与 `--source`、`--comp` 三选一：清单顶层恰含 `performance`（四个必填字段、不带乐谱、至多 2000 条事件）、`races`（相邻、同 tick、同通道的可互换事件对，每通道至多六对）、`checkpoints`（至多 2000 项，输入无需排序）。

核验会枚举每对的保留/互换选择并按通道独立重放：解释必须走完整段录制、满足同音高 FIFO 与踏板语义、没有无对应按键的 off，并符合沿途全部检查点计数；局部前缀能重放不构成证据。输出 `status`、`channels`、`performance`、`report`：

- `certified`：每通道可行且每个声部的释放/止音五元组证据唯一。取所有对上最小的合法翻转选择重排事件，输出可直接回放的完整 `performance` 与旧 `--source` 完整 `report`；即使仍有多个合法事件次序，只要证据唯一就允许发布。
- `ambiguous`：所有通道都可行，但至少一个声部存在多个各带可重放见证的备选；`performance`、`report` 为 null，不交付猜测的播放版本。
- `inconsistent`：至少一个通道没有可行解释；诊断照常发布，其他通道证据不丢失，`performance`、`report` 为 null。

每个备选的 `flips` 是能重现该五元组且满足本通道全部观测的最小见证（被翻转对在原事件数组中的从 0 开始编号，空数组表示原顺序即可）。清单的结构、类型、身份、边界错误退出 2；无可行解释是审计结果而非输入错误。字段与规则细节见契约。

## 离线用量结算

`--settlement BATCH --output RESULT` 按录次给音轨供稿人结算发声费。首批出账；续批加 `--previous PREVIOUS_RESULT`，只开相对已入账结果的差额。`--previous` 仅可用于本模式，与其余四种模式互斥。

对一份录次的每个声部按其自身速度图精确积分半开区间的发声时长；同一时刻多位供稿人发声则平分，同一人多个声部或通道仍只占一份。音量、按键时长与峰值复音都不参与计费。所有录次的精确用量先按供稿人求和，再对整个项目做一次整数分下取整与尾分分配，绝不按录次分别舍入。批次用 `upsert` 整份替换一个录次的演奏与通道归属，用 `withdraw` 撤下仍在该录次（保留身份，可后续恢复）。同一 `batch_id` 相同内容重放返回 `replayed` 且不再入账，不同内容冲突。`checkpoint` 保存派生用量与已入账累计额，使旧事件文件离线归档后仍能继续结算。

演示（费率每秒一分）：

```sh
.venv/bin/pedal-performance --settlement fixtures/settlement-batch-1.json \
  --output run/settle-1.json
.venv/bin/pedal-performance --settlement fixtures/settlement-batch-2.json \
  --previous run/settle-1.json --output run/settle-2.json
```

首批用量为 `a` 0.5 秒、`b` 0.75 秒、`c` 0.25 秒，项目只有一分归 `b`；第二批只提交录次甲的新版本（`b` 在 0.5 秒止音），项目精确总额不变但三人小数余数相同，唯一一分改归 `a`，差额为 `b` -1、`a` +1。字段、边界与衔接规则见契约。

## 按键版导出

`--key-only MANIFEST --output RESULT` 与其余模式互斥，面向不认踏板、同一音高在一轨上同时至多一键发声的按键消息教学终端。它把踏板决定的实际发声时长烘焙进普通 on/off 消息，并按每轨预先装载的音色及装载开销，选出能完整播放整份录次的最低代价配置。

清单顶层恰含 `performance`、`channel_programs`、`lanes`。`channel_programs` 把源通道映射到音色（有 on 的通道必须有映射），`lanes` 给出每轨可装音色及其代价。先按旧语义求每个声部的 `[起音, 实际止音)`；某音色需要的轨数 `min_lanes` 由所有映射到它的源通道合并后同音高区间的最大重叠决定，重叠的不同音高不额外占轨。设备规划要求每种音色恰好得到 `min_lanes` 条轨，在能完整导出的配置中按总装载代价、再按轨号升序的音色序列字典序取最小；配置确定后按（起音、声部 ID）取字典序最小的轨号编排。零时长声部登记但不播放。输出 `demands`、`omitted_zero_voice_ids`、`configuration`、`total_load_cost`、`voice_routes`，以及可直接交给旧 `--source` 的四字段 `performance`（无踏板事件，同 tick 先 off 后 on，同一键可在止音点立即重起）。设备放不下时发布带 `demands` 的 `no_layout`，其余四项为 null；输入错误退出 2。字段与规则细节见契约。

演示 `fixtures/input-keyonly.json`：音色 `P` 的同音高在轨上重叠、`Q` 只在轨 0 可装，摘要为：

```json
{
  "status": "ok",
  "demands": 2,
  "omitted_zero_voices": 0,
  "total_load_cost": 9,
  "voices": 3
}
```

## 当前边界

仅自定义JSON事件；不解码MIDI、不合成音频、不模拟真实钢琴物理。 实际后续需求可从分谱与踏板清理沿既有身份和结果集成；当前未实现这些扩展，也不保证固定数量的未来任务。
