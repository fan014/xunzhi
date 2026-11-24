# 循知 · 演示资料

本目录用于比赛演示与人工复核，不包含第三方版权素材。

## 素材来源

| 素材 | 路径 | 用途 |
| --- | --- | --- |
| 离线示例讲义与题库 | `../seeds/offline_sample.json` | 无 Key 演示完整闭环 |
| 原创拓扑图 | `../app/static/offline-topology.svg` | 离线示例引用定位 |
| 真实生成用拓扑图 | `../tests/fixtures/images/topology.png` | 上传后生成学习包 |
| 三页 PDF | `../tests/fixtures/pdf/sample_3pages.pdf` | 文字页/矢量页/扫描页演示 |
| 文本样例 | `../tests/fixtures/text/note.txt` | TXT/Markdown 导入 |
| 模型回放 | `../tests/fixtures/model_output/*.json` | 自动测试替身，不用于真实演示 |

## 重建方式

```bat
.venv\Scripts\python tests\fixtures\make_fixtures.py
```

离线示例无需重建，运行期读取 `seeds/offline_sample.json`。

## 演示路径

1. 无 Key 安全演示：`http://127.0.0.1:8000/offline`。
2. 真实生成演示：配置 Key 后，首页上传拓扑图并填写场景 A 进度。
3. 演示脚本见 `三分钟演示稿.md`。

## 边界

- 离线示例与真实资料严格隔离，不写真实学习记录。
- 真实模型未配置或网络不稳定时，优先使用离线示例，不现场调试 Key。
- 本目录不包含任何赛事提交物或报名信息。