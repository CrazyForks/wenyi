# 配置说明

程序读取当前工作目录的 `config.yaml`。配置文件不存在时会自动创建带注释的默认文件。

## 语言

```yaml
language:
  source: auto
  target: zh
```

`source: auto` 会调用模型识别源语言；也可以写死 ISO 639-1 代码，例如 `ja`、`en`、`ko`、`ru`、`fr`、`de`、`es`。目标语言目前为简体中文。

## 模型

```yaml
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  timeout: 600
  max_retries: 4
```

API Key 从 `api_key_env` 指定的环境变量读取，避免把密钥写进配置并提交到仓库。`tiers` 下的 `strong`、`cheap`、`fast` 分别用于高质量翻译、审校判断和较机械的任务；缺少某档时按 `fast -> cheap -> strong` 回退。

离线测试或调试可将 `llm.provider` 改为 `fake`，此时不会发网络请求。

## 流水线

```yaml
pipeline:
  review: true
  autofix_severe: false
  polish: true
  backtranslate_sample: 0
  consistency_qa: false
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  review_concurrency: 4
  glossary_scope: chapter
```

- `review`：每章翻译结束后检查漏译、误译、术语和人称问题。
- `autofix_severe`：自动重译并采纳通过校验的漏译、误译等严重问题。
- `polish`：翻译后再调用强模型润色，质量可能提升，但显著增加耗时和成本。
- `backtranslate_sample`：回译抽检比例，`0` 为关闭。
- `consistency_qa`：全书完成后进行跨章术语、人称、语气和标点检查。
- `rolling_context_segments`：每批翻译附带的前文译文段数。
- `book_understanding`：预扫全书，生成章节梗概和全书概览。
- `prescan_concurrency`：预扫章节梗概的并发数。
- `review_concurrency`：章末审校分块的并发数；设为 `1` 时串行审校。
- `glossary_scope`：`chapter` 仅带本章相关术语和锁定人物，`full` 带全量术语表。

命令行的 `--polish`、`--no-polish`、`--qa`、`--no-qa` 会覆盖对应配置。

## 输出

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  about_page: true
```

- `mono`：生成单语中文版，文件名为 `<书名>.zh.epub`。
- `bilingual`：生成原文与译文对照版，文件名为 `<书名>.zh-bi.epub`。
- `bilingual_order`：`target_first` 表示译文在上，`source_first` 表示原文在上。
- `about_page`：在书籍末尾附加“关于此翻译”项目说明页；设为 `false` 可关闭。

默认只生成单语版；使用 `--bilingual` 可同时生成双语版，配置和命令行也可组合为仅生成双语版。

## 切分、敬称与路径

```yaml
segment:
  max_chars_per_batch: 1800
  max_chars_per_segment: 1200

honorific:
  strategy: keep_style

punctuation:
  normalize: true

paths:
  state_dir: state
```

- `max_chars_per_batch`：单个模型翻译批次的目标字符数。
- `max_chars_per_segment`：超长段落的拆分阈值。
- `honorific.strategy`：日语源文本的敬称处理策略，可选 `keep_style`、`normalize`、`drop`。
- `punctuation.normalize`：统一简体中文大陆常用全角标点。
- `state_dir`：断点、章节产物、术语库和报告的位置。
