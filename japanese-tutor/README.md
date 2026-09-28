# Japanese Tutor MVP

一个通过本机 Codex + QQ Bridge 运行的长期日语学习 Agent。第一阶段已经覆盖最小闭环：

```text
下载/导入可信资料 → 初始化学习者 → 选择新课与到期复习
          → 生成每日课程 → 记录答题结果 → 更新后续复习
```

它不需要向量数据库，也不调用额外的 OpenAI API。Codex 负责对话式教学；本项目里的 Python 工具负责可复现的检索、课程选择和长期状态。

## 快速开始

以下命令在本目录执行。可以直接使用 bridge 已有、且带 PyYAML 的 Python：

```bash
../AI-Bridge-QQrobot-claude/.venv-codex/bin/python scripts/download_sources.py grammar
../AI-Bridge-QQrobot-claude/.venv-codex/bin/python scripts/init_learner.py owner --name Kazusa
../AI-Bridge-QQrobot-claude/.venv-codex/bin/python scripts/generate_daily_lesson.py owner
```

生成结果在 `lessons/owner/YYYY-MM-DD.md`。每天包含 1 个语法点、5～8 个单词和 3 个表达；每个单词都有日语、假名、中文词性、中文释义与例句。“今日表达”和“今日单词”的例句会把汉字标为 `漢字（かんじ）`，并保留送假名，例如 `眠（ねむ）らなければ`。可见来源统一收在末尾 `Source`，精确来源 ID 和答案保存在同名 JSON 中。课程与个人进度默认不进入 Git。

完成练习后记录结果：

```bash
../AI-Bridge-QQrobot-claude/.venv-codex/bin/python scripts/record_lesson_result.py owner \
  --lesson 2026-08-26 --score 3 --total 4 \
  --wrong 'grammar:N5:lesson01_判断句与助词基础:1'
```

错题会进入 `learner/data/<id>/mistakes.jsonl` 和复习队列。语法与单词都会按到期时间出现在后续“今日复习”中，并且至少需要五次成功复习才标为掌握。

群共享课程批改时，用群课程元数据更新回答者自己的学习状态：

```bash
python scripts/record_lesson_result.py qq_MEMBER_ID \
  --source-learner-id group_CURRICULUM_ID \
  --lesson 2026-08-26 --score 3 --total 4
```

## QQ 中使用

先由主人私聊机器人切换工作目录：

```text
/cd /absolute/path/to/Nihon_go_robot/japanese-tutor
```

然后可私聊：

```text
开始今天的日语课程
```

或在已经配置为必须 @ 机器人的小群里发送：

```text
@机器人 我想开始今天的日语课程
```

Bridge 会给每个群成员附加稳定、匿名的 `learner_id`。`AGENTS.md` 指示 Codex 用它隔离学习状态；昵称变化不会丢失进度，原始 QQ OpenID 不会写入 learner 文件。群内回复仍会在开头 @ 触发者。

群专属 Codex session 是通用 Agent，同时保留日语学习和课程管理能力。它不会再因为问题与日语无关而拒绝，只对政治、暴力、色情或露骨性内容作明确拒绝；该判断由 Codex 根据群 session 的开发者指令完成，不使用关键词硬编码。主人的私聊 session 不受这项群聊额外限制。

Bridge 在收到机器人入群事件时注册该群并创建干净的群专属 Codex session；若平台漏发事件，首次 @ 消息会兜底初始化。此后每天 `Asia/Shanghai` 09:00 自动生成群共享课程，按“今日复习、今日表达、今日语法、今日单词、小练习、Source”的顺序逐节发送，每个 `##` 是独立气泡。错过 09:00 的服务会在恢复后补发；中途发送失败会从未完成的章节续传，机器人退群后停止推送。

同一群、同一日期首次生成的 Markdown/JSON 会作为不可变课程快照落盘，普通调用不会重新生成。成员可以自然地说“把今天的共享课再发一遍”或只要求重看单词、语法等章节；意图与章节选择由 Codex 理解，Bridge 再从快照中发送相应 `##` 内容，不使用中文关键词表，也不让模型临时改写课程正文。即使群在 09:00 后才登记，也能按需生成并发布当天快照。

## 数据源

详情见 `sources/sources.yaml`。

- Japanese Grammar Notes：默认下载，CC BY 4.0，主 curriculum 和首节课的语法/中日词表/例句来源。
- JMdict / EDRDG：可选下载，CC BY-SA 4.0，用于词形、读音、词性和英文释义核验；许可证要求保留署名并提供定期更新流程。
- Tatoeba：可选下载，CC BY 2.0 FR（部分记录为 CC0），用于日中句对；导入后保留双方 sentence id 与 contributor。
- UniDic-lite 2.1.2：随 Python 依赖安装，BSD，用于在本地分词并生成汉字的平假名标注；当天课程词表中的标准读音优先。
- Tae Kim：CC BY-NC-SA 3.0 US。MVP 仅登记为手工二级参考，不自动复制进主知识库。

主语法教材通过 Git sparse checkout 只取 `grammar/` 和许可证等必要文件，因此默认下载。JMdict 与 Tatoeba 数据较大，按需执行：

```bash
python scripts/download_sources.py jmdict
python scripts/import_jmdict.py knowledge/raw/JMdict.gz

python scripts/download_sources.py tatoeba
python scripts/import_tatoeba.py \
  --japanese knowledge/raw/jpn_sentences_detailed.tsv.bz2 \
  --chinese knowledge/raw/cmn_sentences_detailed.tsv.bz2 \
  --links knowledge/raw/links.tar.bz2
```

下载快照的 URL、时间、大小和 SHA-256 会记录在 `knowledge/source_snapshots.json`。原始数据和 SQLite 均被忽略，可随时从脚本重建。

## 目录

```text
japanese-tutor/
├── AGENTS.md                 # Codex 的教学和状态规则
├── knowledge/                # 下载资料与 SQLite（大文件不提交）
├── learner/templates/        # 默认 N5 学习配置
├── learner/data/             # 每位用户的私有状态（不提交）
├── lessons/                  # 每日 Markdown + 答案元数据（不提交）
├── scripts/                  # 下载、导入、检索、生成、记录工具
├── sources/sources.yaml      # 来源/许可证/更新策略
├── src/japanese_tutor/       # 核心实现
└── tests/
```

## 测试

```bash
../AI-Bridge-QQrobot-claude/.venv-codex/bin/python -m pytest
```

当前 MVP 仍有意不包含向量数据库、复杂 UI 和自动语音/图片课程。自动定时推送由 QQ Bridge 负责，课程生成和学习状态仍由本目录中的可测试工具负责。
