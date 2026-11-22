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

# 多录次剪辑 v1（--comp）

`pedal-performance --comp MANIFEST --output RESULT` 与 `--source` 互斥，必须且只能给一个；旧模式的报告、可选乐谱评估与退出行为不变。清单与结果均为 UTF-8 JSON，沿用严格读取（重复键、非有限常量、超 8 MB 拒绝）与原子新建发布。

清单顶层恰含 `cuts`、`takes`、`min_duration_us`、`max_duration_us`、`max_switches`。`takes` 1..4 份，每份恰含 `take_id`（标识符格式，清单内唯一）、`performance`、`section_costs`。`performance` 使用演奏 v1 的四个必填字段，不带 `score`；各录次 ppq 与 end_tick 必须相同，速度图可不同；事件 ID 只在本录次内唯一。每份录次沿用单录次校验与事件因果规则；任何录次出现无法匹配的 off，整份清单无效，即使该录次未被选用。全部录次合计最多 8000 条事件、980 个速度点。

`cuts` 为严格递增整数 tick 序列，从 0 到共有的 end_tick，划出 1..12 个相邻段；每段含左端 tick、不含右端 tick，最后一段额外包含 end_tick 上的事件。`section_costs` 长度等于段数，每项为 0..10⁹ 整数或 null（该录次此段被弃用）。`min_duration_us`、`max_duration_us` 为 0..6×10¹⁴ 整数，上限不小于下限，区间含两端。`max_switches` 为 0..段数−1 的整数。字段缺失、额外字段、类型或范围错误均使整份清单无效，退出 2，不发布结果。

`cut_diagnostics` 按切点再按 take_id 排列，覆盖每个内部切点（不含 0 与 end_tick）与每个录次：`held_voice_ids`（仍按键声部）、`sustained_voice_ids`（已松键仍在踏板延音声部）、`pedal_down_channels`（踩下踏板的通道），分别按 ID 或通道排序。状态取自即将处理该 tick 首个事件之前：切点上的事件尚未生效，后续事件与 end_tick 强制止音也不会提前消除活跃声部；没有恰落在该 tick 的事件时同样给出状态。

计划按顺序为每段选一个代价非 null 的录次。相邻段同录次直接保持因果连续；换录次仅当两个录次在该切点都没有仍按键或仍延音的声部才允许，单纯踏板踩下不阻止。换录次数不超过 `max_switches`。每段时长是所选录次速度图在该段左右端之间的精确积分（无事件的段同样计时），总时长为各段精确和，不先舍入，须落在时长区间内。可行计划中依次最小化总代价、换录次数、按段排列的 take_id 序列字典序；结果与录次在清单中的排列无关。

输出恰含 `status`、`cut_diagnostics`、`plan`、`performance`、`event_origins`、`report`。有解时 `status` 为 `ok`：`plan` 含各段 `start_tick`、`end_tick`、`take_id`、`cost`、`duration_us`，以及 `total_cost`、`total_duration_us`、`switches`；精确时长沿用整数或最简分数字符串。无解时 `status` 为 `no_plan`，诊断照常发布，其余四项为 null，退出 0。

`performance` 可直接作为旧命令的 `--source`，保持共有 tick 坐标。速度图输出 tick 0 的速度与此后会影响播放时长的速度变化：段内沿用所选录次实际生效的速度，段首 tick 以右侧所选录次为准。所选段的原事件全部保留，同 tick 继承输入顺序；事件 ID 重新分配为 `e0`、`e1`、… 的全局唯一标识符，可重复生成。换录次时，对两侧切点前踏板状态不同的每个通道补一条踏板事件，调到进入录次的切点前状态；补发事件按通道升序放在切点 tick，先于选中录次在该 tick 的全部原事件；不补发按键事件。`event_origins` 按输出顺序逐条对应：`output_id`、原 `take_id`、原 `event_id`、`patched`；补发踏板另标 `tick`、`channel`，两项原身份为 null。`report` 等于旧模式处理该合成 `performance` 的完整报告，其 `duration_us` 等于计划总时长。

# 录制顺序核验 v1（--capture-audit）

`pedal-performance --capture-audit MANIFEST --output RESULT` 与 `--source`、`--comp` 互斥；旧两种模式契约不变。清单与结果均为 UTF-8 JSON，沿用严格读取（重复键、非有限常量、超 8 MB 拒绝）、原子新建发布与不覆盖目标；结构、类型、身份、边界错误退出 2 且不发布。结构合法却没有任何可行解释是审计结果（退出 0），不是输入错误。

清单顶层恰含 `performance`、`races`、`checkpoints`。`performance` 使用演奏 v1 的四个必填字段，不带 `score`，事件最多 2000 条；沿用既有 ppq、end_tick、tempos、事件范围、身份与非递减 tick 顺序校验，但数组中的因果错误（没有对应按键的 off 等）推迟到重放解释时判定，不在核验前否决。

`races` 每项是两个事件 ID，按原数组顺序列出：两事件须相邻、同 tick、同通道，一个事件至多参加一对；各对按其首事件在原数组中的位置严格递增，每通道至多六对。每对只有原样保留或整体互换两种选择，其余事件及相对顺序不变；互换不改动 ID、tick、速度或事件内容。`checkpoints` 至多 2000 项，每项恰含 `tick`（0..end_tick）、`channel`（0..15）、`held`、`sustained`（均为 0..2000 整数），同一 tick 与通道组合不得重复，输入不要求排序。计数时点是该 tick 全部事件处理之后、录制结束强制止音之前；没有事件的 tick 也可观察。仍按键与已松键但靠踏板维持发声的声部各计一次，同音高多声部分别计数；只踩踏板而无延音时延音数仍为零。

一份解释是一组翻转选择。从头按它重放时必须满足同音高 FIFO、踏板与止音语义，不能出现没有对应按键的 off，且沿途所有检查点的两个计数必须符合观测；不要求录制结束前所有按键都已释放。只有能走完整个录制并符合本通道全部观测的解释可以作证，局部前缀可重放不构成证据。各通道只依赖本通道的事件与观测独立审计：一个通道没有解释时仍保留其他通道的审计结果。

输出恰含 `status`、`channels`、`performance`、`report`。`channels` 按通道号升序，每个有事件或观测的通道一行，含 `channel`、`feasible`、`voices`；不可行通道给 `feasible: false` 与空 `voices`。可行通道按 `voice_id` 列出每个 on 对应声部，每行含 `voice_id` 与 `alternatives`。每个备选含一个整体五元组 `key_release_tick`、`release_event_id`、`sound_end_tick`、`end_reason`、`end_event_id`（沿用演奏报告的字段与 null 语义，五个字段必须来自同一条解释，不得跨解释拼装；止音 tick 相同但事件或原因不同也分别列出），以及 `flips`：一份能重现该五元组且满足本通道全部观测的见证，列出本通道被翻转对的编号（对在原事件数组中的从 0 开始位置），升序排列；没列出的对保持原顺序，空数组表示原顺序即可重现。同一五元组只出现一次，见证取按本通道各对出现顺序比较、保留早于翻转的最小选择；备选也按该见证顺序排列。

任一通道不可行时 `status` 为 `inconsistent`；否则只要任一声部有不止一个备选，`status` 为 `ambiguous`。这两种状态正常发布审计 JSON，但 `performance`、`report` 均为 null，不交付猜测的播放版本。若每通道可行且每个声部的五元组证据都唯一，`status` 为 `certified`，即使仍有多个合法事件次序也允许发布：取所有对上按编号比较、保留早于翻转的最小合法整份翻转选择，用它重排事件后输出完整 `performance`，并给出旧 `--source` 处理它得到的完整 `report`。证据唯一性按声部 ID 判断，不因同 tick 两条起音在旧报告中的展示位置不同而改变。


# 离线用量结算 v1（--settlement）

`pedal-performance --settlement BATCH --output RESULT` 首次出账；续批用 `pedal-performance --settlement BATCH --previous PREVIOUS_RESULT --output RESULT`。`--previous` 仅可用于本模式；五种模式互斥。一次结算服务同一素材项目，费用用整数分记账，不实际发起付款。旧三种模式的输入输出与行为不变。批次、上一份结果与输出均为 UTF-8 JSON，沿用严格读取（重复键、非有限常量、超 8 MB 拒绝）、原子新建发布与不覆盖目标；输入、衔接或发布失败退出 2，不留下部分结果，不改动上一份结果。

## 批次

批次顶层恰含 `project_id`、`batch_id`、`base_batch_id`、`cents_per_second`、`changes`。`project_id`、`batch_id`、`base_batch_id`（非 null 时）、`recording_id`、`owner_id` 均沿用 1..64 位标识符格式；`base_batch_id` 另可为 null。`cents_per_second` 为 1..1000000 的整数分/秒，在项目首次出账后固定。

`changes` 是 1..32 条变更，同一录次在一个批次至多出现一次：

- `upsert` 恰含 `op`（`upsert`）、`recording_id`、`revision`、`performance`、`channel_owners`。`performance` 使用演奏 v1 的四个必填字段、不带 `score`，至多 2000 条事件；同 tick 顺序有意义，沿用旧版范围、身份与因果校验（无法匹配的 off 使整批无效）。不同录次之间的演奏事件 ID 可以相同。`channel_owners` 是 0..16 项数组，每项恰含 `channel`（0..15，同一表内不重复）、`owner_id`。同一供稿人可管多个通道；所有有起音事件的通道都必须有归属，允许为无起音通道指定归属。每次 upsert 整份替换该录次演奏及其通道归属，不与上一版合并。
- `withdraw` 恰含 `op`（`withdraw`）、`recording_id`、`revision`。只有当前仍在账的录次可撤回。撤回删除其用量，但保留录次身份和版本号。

新录次只可用版本 1 upsert；任何已见录次的下一次变更版本号必须恰好加 1，包括撤回后恢复。版本号是 1..256 的整数。项目最多登记过 32 个不同录次 ID（含已撤回的），最多接受 256 个不同批次。

## 用量

从每个声部的起音到实际止音计费，使用半开区间：松键后的踏板延音照计，未提前止音（`end_of_take`）的声部按录次结尾止音，零时长不计。用该录次自己的速度图与 ppq 换算精确微秒，不用按键时长，也不用峰值复音。对一份录次的任一段正时长，若同时有 k 位不同供稿人发声，每位分得该段时长的 1/k；同一人叠了多个声部或通道仍只占一份。静音不计。录次各算各的，从不同录次分得的时长按供稿人求和；不同录次都从 tick 0 起，不共享播放时间轴。全程使用精确 `Fraction` 微秒，中途不舍入。

## 从用量到入账

对变更后的**整个项目**计算每人当前应付目标：各供稿人的精确用量乘 `cents_per_second` 并换算成分（`usage_us * rate / 1000000`）。先给每人下取整；项目应付总分是所有人精确金额之和下取整。差出的整数分依次给小数余数较大的供稿人，同余数按 `owner_id` 字典序升序，每人至多补一分。不按录次分别取整。

每人的本批差额是当前应付目标减去前批已入账累计额（初次为零）。已无用量的人当前目标为零。只列非零差额，允许负数冲减；即使某人所有录次都没变，项目内尾分重新分配也可能使其需要入账。

## 批次衔接与交付

没有上一份结果时，只接受 `base_batch_id: null` 的首批；有上一份结果时，`project_id` 与 `cents_per_second` 必须与它一致，且 `base_batch_id` 必须等于上一份结果的 `head_batch_id`。

上一份结果由本模式生成且不得修改。续算前校验其 `schema_version`、必填字段、`checkpoint` 结构、身份，以及 `checkpoint` 与顶层 `balances`/`total_cents`/`adjustments` 的一致性；拒绝不能用于续算的结果。不要求数字签名或跨文件分支协调。

已接受过的 `batch_id` 优先按重放处理：相同内容返回 `replayed`、差额为空，当前结算结果、批次记录和最新批次不回退。相同 ID 不同内容是冲突（退出 2）。比较内容时忽略 JSON 对象键顺序、`changes` 的排列与 `channel_owners` 的排列，仍保留同 tick 事件次序等演奏数组的原有语义，并包含原 `base_batch_id`。允许重放非最新的已接受批次，不因它的原基准落后或录次版本已变而重入账或报基准过期。新 ID 使用过期基准则拒绝。

变更先整体校验再对外发布，任一变更非法就不产出差额或新状态。合规的新批次即使没有非零差额也作为 `applied` 登记。

## 结果

结果至少含 `schema_version`（1）、`status`（`applied` 或 `replayed`）、`project_id`、`batch_id`（本次请求）、`head_batch_id`（当前最新）、`cents_per_second`、`total_cents`、`balances`、`adjustments`、`checkpoint`。

`balances` 列出当前精确用量大于零的各供稿人，每行 `owner_id`、`usage_us`（精确的整数字符串或最简分数字符串）、`payable_cents`；应付总分等于各行 `payable_cents` 之和。`adjustments` 每行 `owner_id`、`previous_cents`、`payable_cents`、`delta_cents`，金额字段为整数，仅列非零差额。两个列表均按 `owner_id` 字典序升序。

`checkpoint` 内部格式自定，输出可直接作为下一次的 `--previous`，不依赖旧路径、进程内缓存或外部服务，也不复制旧录次原始事件。它保存每个录次的版本、是否在账与其按供稿人的精确用量，以及已入账累计额和已接受批次的指纹，足以在旧事件文件离线后继续结算。

# 按键版导出 v1（--key-only）

`pedal-performance --key-only MANIFEST --output RESULT` 与 `--source`、`--comp`、`--capture-audit`、`--settlement` 互斥；旧四种模式的输入输出与行为不变。清单与结果均为 UTF-8 JSON，沿用严格读取（重复键、非有限常量、超 8 MB 拒绝）、原子新建发布与不覆盖目标；输入错误（包括设备不会用到的源通道上的错误）或发布失败退出 2 且不留下部分结果。设备放不下但输入本身合法时正常发布 `no_layout`（退出 0），不缩短尾音、不替换音色。本模式不接入真实设备。

## 清单

顶层恰含 `performance`、`channel_programs`、`lanes`。

`performance` 是演奏 v1 的四个必填字段，不带 `score`，事件最多 4000 条。沿用原有 ppq、end_tick、tempos、事件范围、身份与非递减 tick 顺序校验，以及同 tick 输入顺序、同通道同音高 FIFO 释放、没有对应起音的 off 即输入错误的因果规则。声部的实际发声区间 `[start_tick, sound_end_tick)` 沿用演奏 v1：松键后踏板延音照样计入，仍未止音的按录次结尾止音；零时长声部（起音 tick 等于实际止音 tick）无需播放但登记其 ID。

`channel_programs` 为 0..16 项，每项恰含 `channel`（0..15 整数，表内不重复）与 `program_id`（音色标识）。允许多个源通道指定同一音色，允许为无起音通道指定音色。所有有 on 事件的源通道都必须有映射，包括只产生零时长音的通道；缺映射是输入错误。

`lanes` 为 1..12 项，每项恰含 `lane`（0..15 整数，表内不重复，输出时用作通道号）与 `program_costs`（1..8 项）。`program_costs` 每项恰含 `program_id` 与 `load_cost`（1..1,000,000 整数）；同一轨中音色不得重复，未列出的音色不能装到该轨。未装载的轨不收费。音色映射与设备表中列出的音色 ID 合计最多 8 个不同值。两个表与各轨 `program_costs` 的排列都没有业务意义。音色 ID 沿用标识符格式。

## 发声保留与设备规划

每个源声部的实际发声区间必须完整使用一条轨：不许中途换轨、不许拆音。该轨的音色必须是源通道指定的音色。不同源通道若映射到同一音色，可以共用轨，但其同音高的发声区间在该轨上不能重叠（前一声部恰在后一声部起音时止音不算重叠）。不同音高不争用轨。设备规划针对整份录次：一条轨在开播前装载一次音色，此后即使整条轨已静音也不能改装去承接后一段的另一音色。

某音色的 `min_lanes`（忽略设备表）是按上述键位规则、把所有映射到该音色的源通道合并考虑时，其同音高发声区间至少需要多少条轨；不同音高的重叠不抬高该数。这是单音色的独立需求，不保证整份设备表可行。

设备规划先要求每种有正时长声部的音色获得恰好 `min_lanes` 条轨（多给只会增加正代价且不会解锁任何可行编排），可装载该音色的轨各自只能装一种音色。在所有能完整导出的配置中选总装载代价最小者，每个已装载轨只算一次；代价相同时，把输入轨按轨号升序排列，比较对应 `program_id` 序列的字典序，取最小者，未装载视为大于任何音色 ID。配置确定后，把正时长源声部按（起音 tick、源声部 ID）排列，在所有符合该配置的完整编排中取轨号序列字典序最小者。不得靠提前截断声部或合并同音来降低成本。

## 结果

输出恰含 `status`、`demands`、`omitted_zero_voice_ids`、`configuration`、`total_load_cost`、`voice_routes`、`performance`。

`status` 为 `ok` 或 `no_layout`。`demands` 为每个有正时长声部的音色一行，含 `program_id` 与 `min_lanes`，按音色 ID 升序。`omitted_zero_voice_ids` 列出零时长源声部 ID，按 ID 升序。

有解时 `status` 为 `ok`：`configuration` 按轨号升序列出全部输入轨，每行含 `lane` 与 `program_id`（未装载为 null）；`total_load_cost` 为该配置总装载代价；`voice_routes` 按编排的声部顺序，每行含 `source_voice_id`、`lane`、`on_event_id`、`off_event_id`。没有正时长声部时仍可成功导出，全部轨未装载、代价为零、编排为空。

有解时 `performance` 是可直接作为旧 `--source` 的四字段演奏，原样保留 `ppq`、`end_tick` 与速度图。每条编排记录对应唯一一对生成的 on/off：on 用原音高和力度、在原起音 tick 发出，off 在原实际止音 tick 发出，二者都走编排指定的轨；不输出踏板事件。生成的 ID 稳定且符合原格式。同 tick 的 off 一律在 on 之前，同类按轨号、音高、源声部 ID 升序，使同一键能在止音点立即重新起音。用旧命令重放并经编排表逐一回查时，发声音高、力度、起音 tick 与实际止音 tick 须与原声部精确相符；不要求保留原松键 tick 或止音原因。

合法但无法完整编排时 `status` 为 `no_layout`，保留 `demands` 与零时长记录，其余四个交付字段 `configuration`、`total_load_cost`、`voice_routes`、`performance` 均为 null，退出 0。
