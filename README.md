# train_cal2

福州东调车计划求解项目。主链只有四阶段求解、共享物理模型、独立回放验证、
HTTP API、Streamlit 回放界面和人工计划恢复。

## 目录

```text
plan_api/                  四阶段 HTTP API 与任务管理
scripts/stage1_simple/     第一阶段编组与前场服务
scripts/stage2_simple/     卸轮翻库与大库出库编组
scripts/stage3_simple/     大库入库与台位对齐
scripts/stage4_simple/     前场剩余债务闭合
scripts/solver_vnext/      四阶段共享物理模型
replay_validator.py        独立计划回放验证
app.py                     全流程、人工计划和分阶段可视化
data/truth2, data/truth3   回归输入
data/人工调车数据          人工回放原始作业单
tests/                     核心回归测试
```

## 执行模型

项目只有一套 Stage1-4 求解链。各阶段 CLI 的 `input` 参数统一采用：

- JSON 文件：只运行该案例；
- truth 目录：批量运行目录中的全部 `validation_*.json`；
- 不存在或不含案例的目录直接报错，不生成空批次；
- Stage2-4 只接受上游 `complete` 产物，不存在继续求解 partial 的开关。

所有阶段都必须显式指定 `--out`；Stage2-4 还必须显式指定对应的上游输出目录，
避免 truth2、truth3 或单案运行之间误读默认产物。

阶段结果统一使用 `complete`、`partial`、`unavailable`、`error`：真正进入求解但未
完成才是 `partial`，上游产物不可用是 `unavailable`，程序异常是 `error`。各阶段
CLI 仅在批次内所有案例均 `complete` 时返回退出码 0。

四阶段单案由 `POST /api/plan/generate` 串行完成，四阶段批量由
`POST /api/plan/generate/batch` 提交。两个接口调用同一个 pipeline，不存在备用求解器。

`app.py` 分别提供全流程、Stage1、Stage2、Stage3、Stage4 和人工计划回放；选择案例后
按该案例的实际 Operations 逐勾回放。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

只部署 API 时使用更小的依赖集：

```bash
.venv/bin/pip install -r requirements-api.txt
```

## API

```bash
.venv/bin/uvicorn plan_api.server:app --host 127.0.0.1 --port 8000
```

接口契约见 [docs/接口文档.md](docs/接口文档.md)。

## 可视化

```bash
.venv/bin/streamlit run app.py
```

界面读取 artifacts/fullflow_current 和 artifacts/manual_restored_interface。
产物不存在时先运行对应求解或人工恢复命令。

## 人工回放

```bash
.venv/bin/python scripts/restore_manual_interface_responses.py \
  --root . \
  --output-dir artifacts/manual_restored_interface
```

该入口内部复用人工计划标签清洗和身份回放，不把人工计划视为严格物理真值。

## 验证

```bash
python3 -m pytest -q
```

单个响应可独立回放：

```bash
python3 replay_validator.py response.json --request request.json
```
