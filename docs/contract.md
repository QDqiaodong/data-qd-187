# 演奏事件 v1

教学演奏数据使用自定义结构化JSON，不解析MIDI文件，也不输出音频。ppq=每四分音符tick数(1..9600)，end_tick正整数≤10000000；tempos从0开始严格递增，每项是正整数microseconds_per_quarter。每段时间精确积分为tick差×速度/ppq，以整数或分数字符串微秒输出，不用浮点累加。

events最多10000条，id全局唯一，按非递减tick输入；同tick按列表顺序处理，不擅自按种类重排。channel为0..15，on需要pitch(0..127)、velocity(1..127)，off只有pitch；pedal只有布尔down。事件不超过end_tick；tempo点可等于end_tick。未知字段、重复身份、错类型拒绝。

每个on创建独立voice_id=id。同通道同音高可重复按下，off按FIFO释放最早仍按着的声部；找不到声部视为非法输入，整个报告不发布。踏板按通道独立：按键释放时若踏板down，声部进入延音；抬踏板只停止已释放键的延音声部，不停止仍按着的键。再次按同音高不自动截断旧延音。该明确模型不模拟真实钢琴制音器、声衰减、同音高物理共振或标准设备差异。

记录每声部起始、释放键和实际止音时间及因果事件id。end_tick处强制停止所有未止音声部，end_reason=end_of_take；仍按键的key_release_tick=null并列入unfinished_key_ids。结束时已释放但持续踩踏板的声部也在此止音，不伪造off事件。零长声部合法；峰值复音以半开[start,end)区间求和，同tick起止净变化，零长不计。未闭合键是业务诊断，不使CLI失败。

成功原子新建一个完整JSON；已有目标不可覆盖。结构错误或无匹配off退出2并不发布。没有音频设备、联网、持久录制会话或自动音乐理解。

# 乐谱提示评估 v1（可选 score）

输入可另带可选 `score` 对象，字段为 `ppq`、`end_tick`、`tempos`、`cues`，约束与演奏侧一致：ppq 1..9600，end_tick 正整数≤10000000，tempos 从0严格递增且速度为正整数。score 与演奏各自从 tick 0 按自己的速度图积分成微秒再比较，两套 ppq 与速度图互不影响。

每个 cue 恰有 `cue_id`（1..64位标识符，全局唯一）、`channel`(0..15)、`pitch`(0..127)、`start_tick`、`release_tick`（均在 0..score.end_tick 内）、`onset_tolerance_us`、`duration_tolerance_us`（均为 0..10^12 的非负整数）。cues 按 `start_tick` 非递减输入，同 tick 保持输入次序；最多 10000 条。未知字段、缺字段、错类型、重复身份一律拒绝。`bool` 不算整数。

先把演奏按前述契约解析成声部，再逐 channel/pitch 归因：只有 channel 与 pitch 都相同的 cue 与声部才可比；归因必须是保序一对一，不能重复使用 cue 或声部。若有多种方案，依次取匹配数量最多、起音绝对偏差总和最小、可观测按键时值绝对偏差总和最小者；仍并列时取 cue_id 与 voice_id 配对序列（按 cue 次序）字典序最小者。未释放按键的声部在时值总和中贡献 0（不可观测）。

所有时间和比较使用精确整数：两套 ppq 的最小公倍数为 L，所有微秒时间乘以 L 成为整数后再加减与比较，偏差以最简分数字符串输出。不使用浮点累加或浮点舍入决定归因与达标状态。

每条 cue 输出 `voice_id`、`onset_deviation_us`（实际起音−乐谱起音）与 `onset_status`：绝对偏差≤`onset_tolerance_us` 为 `ok`，否则为 `early`（偏差<0）或 `late`。按键时值以演奏侧 `key_release_tick−start_tick` 换算，目标时值以 score 侧 `release_tick−start_tick` 换算，绝对偏差≤`duration_tolerance_us` 为 `ok`，否则 `early`/`late`。无匹配声部时 `voice_id=null`、`onset_status=missed`、`duration_status=not_applicable`、两者偏差为 null。匹配声部在演奏 end_tick 仍未释放按键时保留起音结果，`duration_deviation_us=null`、`duration_status=undetermined`，不伪造释放。声部的 `sound_end_tick`、`end_reason` 与踏板延音只作演奏证据输出，不参与按键时值判断。

有 score 时报告新增 `score_evaluation`：`cues`（按输入顺序，逐条上述字段加 `channel`、`pitch`）、`missed_cue_ids`、`extra_voice_ids`（未匹配声部，即多奏）。`summary` 在原四字段之外增加 `matched_cues`、`missed_cues`、`extra_voices`、`onset_ok`、`duration_ok`、`duration_undetermined`。schema_version、duration_us、voices、unfinished_key_ids 与原有 summary 字段含义不变。没有 score 时输出与旧版完全一致。

score 无效、演奏无效或评估无法完成时整份报告不发布，退出 2；不得留下部分报告。输出仍只原子新建，不覆盖已有目标。

