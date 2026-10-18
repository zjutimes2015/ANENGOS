# ANENGOS 认知体系（Cognition Layer）

> 让 AI 在「已知代码」上改的机制层。三大组件：**策略认知卡**（把代码变成认知）+
> **晋升门**（新代码进入核心层的唯一通道）+ **修改协议**（无依据不改核心层）。
> 入口文档：`docs/cognition/README.md` → `knowledge_map.md`（盘点）→ `cards/`（认知卡）
> → `governance/`（晋升门 + 修改协议）。

## 是什么

截图方法论（HEXIN）三条落地规则：
1. **AI 是杠杆，放大你的认知** —— 认知的载体是「策略认知卡」，不是代码本身
2. **不要让 AI 做决策** —— 晋升门与参数采纳一律由人（你）拍板
3. **真正的策略是让 Codex 在已知代码上改** —— 修改协议强制「依据链」

## 目录

| 路径 | 内容 |
|---|---|
| `knowledge_map.md` | ① 16 agents 策略已知度盘点表（6 要素评分） |
| `cards/ersibang.md` | ② 二四棒策略认知卡（首批种子策略 #1） |
| `cards/multifactor.md` | ② 多因子策略认知卡（首批种子策略 #2） |
| `governance/promotion_gate.md` | ③ 晋升门规则（实验层 → 核心层准入标准） |
| `governance/modification_protocol.md` | ④ 修改协议（依据链铁律） |
| `cron/error_evolution_cron.py` | ⑤ 认知卡状态自动刷新（周日 cron 已含） |

## 认知卡自动刷新（已挂进周日 cron）

`error_evolution_cron.py` 每周日 20:00 自动：
- 跑 error_log（错误进化）
- 生成 `error_evolution_YYYYWW.md`（进化状态）
- **刷新 `cognition/cards/*.md` 的「虚拟盘跟踪」与「已知缺陷」两节**（标注 `<!-- auto-refresh -->` 段）

周一 09:00 `retro_cron.py` 生成周复盘时自动合入进化章节 → 认知闭环每周自动转。

## 使用约定

- 核心层代码改动：必须先读对应认知卡，改动必须附依据链（见修改协议）
- 新策略/新想法：一律先走晋升门（实验层），不得直接改核心层
- 认知卡状态只有三种：`实验层` / `核心层-虚拟盘验证中` / `核心层-已验收`
