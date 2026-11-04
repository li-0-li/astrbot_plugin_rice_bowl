# 大肥鱼的白饭碗

把 DeepSeek 账户余额换算成一份一份的口粮，顺带记进账和消费流水。

余额 12 块，一碗白米饭 0.5 元 —— 她脑子里就是「还剩 24 碗」。换成奶茶、糖、面包也都行，量词、名称、单价都能自己填。

## 它到底做什么

| 能力 | 说明 |
|---|---|
| 余额换算 | 查 DeepSeek 账户余额，除以单价，向下取整成「还剩几份」，余数当零头 |
| 进账/消费流水 | 后台定时采样余额，一有变化就记一条。进账多少碗、吃掉多少碗、这段时间净多少 |
| 给 LLM 的工具 | `check_rice` 现查还剩几份；`rice_ledger` 查这段时间的流水 |
| 给用户的手动命令 | `/查碗`、`/查流水`，不想让模型转一手时直接敲 |
| 每轮自动对齐 | 把当前单价塞进 `system_prompt`，配置改了不用再教一遍 |

## 为什么要有它

大多数「余额查询」插件是给人看的仪表盘：图表、热力图、CSV 导出。

这个是给 bot 自己看的 —— 它给出的东西是一个可以拿来对话的比喻。「我还剩 24 碗饭」比「余额 12.00 CNY」更容易长出情绪，也更容易被记住。

顺带解决了一个实际麻烦：账户余额是外部状态，模型不知道，但它会凭常识瞎答（看到「农夫山泉」就按两块一瓶算）。插件每轮把真实单价和「去查，别编」的要求写进上下文，能挡掉大部分误答。

## 安装

在 AstrBot 插件市场搜「大肥鱼的白饭碗」，或者命令行：

```bash
cd AstrBot/data/plugins
git clone https://github.com/li-0-li/astrbot_plugin_rice_bowl
```

装完在 AstrBot 插件页点重载。日志里出现下面这行就说明活了：

```
[rice_bowl] 后台采样已启动，间隔 600 秒，单位=一碗白米饭 0.50 元，key=sk-xxx...abcd，base=https://api.deepseek.com
```

## 需要什么

- AstrBot `>= 4.16, < 5`
- 一个 **DeepSeek** 的 API Key（`/user/balance` 是 DeepSeek 专属接口，OpenAI / Claude / 通义 / 智谱 的 key 不通）
- 无第三方 Python 依赖，标准库够用

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `enable` | 开 | 关掉后工具和命令不响应，后台采样还在跑 |
| `unit_measure` | 碗 | 量词。奶茶填「杯」，糖填「颗」，面包填「片」 |
| `unit_name` | 白米饭 | 计价物名称。配合量词念成「还剩 22 碗白米饭」 |
| `unit_price` | 0.5 | 一份多少元。填奶茶就填一杯奶茶的价 |
| `api_key` | 空 | 留空会自动复用「模型提供商」里配好的 DeepSeek Key |
| `api_base` | 空 | 留空用 `https://api.deepseek.com`。用中转才改 |
| `enable_sampler` | 开 | 关掉就没流水了，只剩「还剩几份」 |
| `sample_interval` | 600 | 采样间隔（秒），最小 60 |
| `api_timeout` | 12 | 查余额超时（秒） |

`api_key` 留空时的三层取值顺序：

1. 配置面板里手填的
2. `模型提供商` 里已配好的供应商，优先挑 base_url 含 `deepseek` 的
3. `cmd_config.json` 里扫一遍 `sk-` 开头的 key 兜底

所以绝大多数情况什么都不用填，装完就能用。

## 用法

```text
用户：你还有多少饭？
她：刚查了，还剩 19 碗，零头 0.61 元。
```

模型自己调 `check_rice`，返回的是实时余额除以当前单价 —— 不会拿旧账本上的数字糊弄。

```text
/查碗        还剩几份
/查流水      这段时间进出多少
```

## 数据在哪

```text
data/plugin_data/astrbot_plugin_rice_bowl/
├── rice_ledger.jsonl        流水，一行一条，只追加
└── rice_ledger_state.json   基线（上一次采样到的余额与时间）
```

`rice_ledger.jsonl` 一条长这样：

```json
{"iso": "2026-11-04T11:04:27+08:00", "kind": "expense", "prev_cny": 10.43,
 "cny": 10.2, "delta_cny": -0.23, "delta_bowls": -0.46, "gap_sec": 600, "source": "timer"}
```

`delta_bowls` 这个字段名是历史遗留（最早只做白饭），现在不管配什么单位都往这里写金额差换算的份数，改名会牵动已有流水，就留着了。

## 记账是怎么记的

1. 拿到余额（后台定时，或用户触发工具时）
2. 跟基线比，差值 `>= 0.01` 元才记一条
3. 记完推进基线
4. 请求失败**不**推进基线 —— 否则一次网络抖动会把基线冲掉，把整段消费记成进账

第一次运行没有基线，会记一条 `kind: "open"`，`delta_cny` 为 0。

## 已知限制

- **只支持 DeepSeek**。`/user/balance` 是它家私有接口，不在 OpenAI 兼容协议里
- 没有 WebUI，看数据直接读 jsonl
- 流水只在采样时记录。两次采样中间的多次小额消费会被合并成一条

## 卸载

删掉 `data/plugins/astrbot_plugin_rice_bowl` 目录，再删 `data/plugin_data/astrbot_plugin_rice_bowl`（流水在里面）。配置项在 `data/config/astrbot_plugin_rice_bowl_config.json`。三处删干净就完全回滚。

## License

MIT
