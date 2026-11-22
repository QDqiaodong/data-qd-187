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

实际52项测试通过；CLI演示生成JSON，摘要为：

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

## 当前边界

仅自定义JSON事件；不解码MIDI、不合成音频、不模拟真实钢琴物理。 实际后续需求可从分谱与踏板清理沿既有身份和结果集成；当前未实现这些扩展，也不保证固定数量的未来任务。
