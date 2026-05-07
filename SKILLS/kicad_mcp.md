# SKILL: KiCAD MCP 电路设计

适用领域：PCB 设计、硬件原型开发、电子电路自动化。

## 概述

KiCAD MCP Server 是一个基于 Model Context Protocol (MCP) 的服务，允许 AI 助手通过标准协议控制 KiCAD 进行电路设计自动化。提供 **124 个工具**（62 个核心工具 + 路由工具），涵盖项目管理、原理图设计、PCB 布局、布线、导出等完整设计流程。

**核心原则：先绘制原理图，再做 PCB。** 原理图是设计的源头，PCB 布局基于原理图生成。

## 环境要求（已经安装则不用重复安装）

- **KiCAD 10.0**：安装在 `C:\Program Files\KiCad\10.0`
- **Node.js >= 18**：运行 MCP Server
- **KiCAD-MCP-Server**：从 GitHub 克隆并构建
  ```bash
  git clone https://github.com/mixelpixx/KiCAD-MCP-Server.git
  cd KiCAD-MCP-Server
  npm install
  npm run build
  ```

### 关键环境变量

```bash
KICAD_PYTHON=C:\Program Files\KiCad\10.0\bin\python.exe
PYTHONPATH=C:\Program Files\KiCad\10.0\bin\Lib\site-packages
```

**重要**：`KICAD_PYTHON` 必须指向 KiCAD 自带的 Python 解释器，不能使用系统 Python。

## 工具调用方式

### 使用 kicad_mcp_batch 工具

通过 `kicad_mcp_batch` 工具批量调用 KiCAD MCP 操作，支持在同一个会话中执行多个操作并保持状态：

```json
{
  "operations": [
    {"tool_name": "create_project", "params": {"name": "MyBoard", "path": "/path/to/project"}},
    {"tool_name": "open_project", "params": {"filename": "/path/to/project/MyBoard.kicad_pro"}},
    {"tool_name": "save_project", "params": {}}
  ]
}
```

---

## 核心工作流：原理图优先 → PCB

### 阶段总览

```
┌─────────────────────────────────────────────────────────────┐
│  阶段1: 需求分析                                             │
│  与用户讨论电路功能、性能指标、关键芯片选型                    │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段2: 蓝图设计（JSON 落盘）                                 │
│  定义功能模块、元件清单、连接关系                              │
│  输出: blueprint.json（结构化设计文件）                        │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段3: 用户确认                                             │
│  展示蓝图方案，等待用户确认或修改意见                          │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段3.5: 封装齐备性检查                                      │
│  检查蓝图中所有元件的原理图符号和PCB封装是否可用               │
│  三级策略：查找KiCAD库 → 查询JLCPCB → 找相近符号修改          │
│  输出: 封装检查报告，列出缺失项和解决方案                      │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段5: 原理图绘制（模块聚类）                                 │
│  基于蓝图，按功能模块聚类放置元件                              │
│  考虑连线顺畅，调整无源器件位置和方向                          │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段6: 原理图连线                                           │
│  添加网络标签、导线连接、电源符号                              │
│  生成网表验证连接正确性                                       │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段7: PCB 设计                                             │
│  原理图同步到 PCB → 元件布局 → 布线 → DRC → 导出              │
└─────────────────────────────────────────────────────────────┘
```

---

### 阶段 1：需求分析

**目标**：与用户多轮讨论，明确电路设计方案。

**关键问题清单**：
1. 电路功能是什么？（电源管理、信号处理、通信接口等）
2. 工作电压/电流范围？
3. 关键芯片选型？（MCU、驱动芯片、传感器等）
4. 通信接口需求？（UART、SPI、I2C、CAN、USB等）
5. 尺寸限制？
6. 成本要求？

**输出**：设计方案文档（可在草稿本中记录）

---

### 阶段 2：蓝图设计（JSON 落盘）

**目标**：将设计方案转化为结构化 JSON 文件，作为后续所有操作的基准。

**蓝图文件格式** (`blueprint.json`)：

```json
{
  "project_name": "ProjectName",
  "description": "电路描述",
  "version": "1.0",
  "modules": [
    {
      "name": "MCU最小系统",
      "description": "主控芯片及其外围电路",
      "center_x": 100,
      "center_y": 100,
      "components": [
        {
          "ref": "U1",
          "symbol": "Connector:Conn_01x04",
          "value": "ESP32-WROOM-32",
          "footprint": "Module:ESP32-WROOM-32",
          "pins": ["1", "2", "3", "4"],
          "role": "master"
        },
        {
          "ref": "R1",
          "symbol": "Device:R",
          "value": "10k",
          "footprint": "Resistor_SMD:R_0603_1608Metric",
          "pins": ["1", "2"],
          "role": "slave"
        },
        {
          "ref": "C1",
          "symbol": "Device:C",
          "value": "100nF",
          "footprint": "Capacitor_SMD:C_0603_1608Metric",
          "pins": ["1", "2"],
          "role": "slave"
        }
      ],
      "internal_connections": [
        {"from": "U1/1", "to": "R1/1", "net": "EN"},
        {"from": "R1/2", "to": "C1/1", "net": "EN_PULLUP"},
        {"from": "C1/2", "to": "GND", "net": "GND"}
      ]
    },
    {
      "name": "电源管理",
      "description": "DC-DC降压+LDO稳压",
      "center_x": 100,
      "center_y": 200,
      "components": [...],
      "internal_connections": [...]
    }
  ],
  "inter_module_connections": [
    {"from": "MCU最小系统/U1/5", "to": "电源管理/U2/1", "net": "3V3"},
    {"from": "MCU最小系统/C1/2", "to": "电源管理/C5/2", "net": "GND"}
  ],
  "nets": [
    {"name": "GND", "type": "power"},
    {"name": "3V3", "type": "power"},
    {"name": "5V", "type": "power"},
    {"name": "EN", "type": "signal"},
    {"name": "TX", "type": "signal"},
    {"name": "RX", "type": "signal"}
  ],
  "power_symbols": [
    {"ref": "PWR#GND", "symbol": "power_symbol:GND", "count": 10},
    {"ref": "PWR#3V3", "symbol": "power_symbol:+3V3", "count": 5}
  ]
}
```

**关键字段说明**：

| 字段 | 说明 |
|------|------|
| `modules[].name` | 功能模块名称 |
| `modules[].center_x/y` | 模块在原理图中的中心坐标（mm） |
| `modules[].components` | 模块内元件列表 |
| `components[].ref` | 元件位号（R1, C1, U1 等） |
| `components[].symbol` | 符号库引用，格式 `Library:SymbolName` |
| `components[].value` | 元件参数值 |
| `components[].footprint` | PCB 封装 |
| `components[].pins` | 引脚列表 |
| `components[].role` | `master`（大芯片，位置固定）或 `slave`（无源器件，可调整） |
| `internal_connections` | 模块内部连接关系 |
| `inter_module_connections` | 跨模块连接关系 |
| `nets` | 全局网络定义 |

**落盘操作**：
1. 将蓝图 JSON 写入 `$RUN_DIR/artifacts/blueprint.json`
2. 向用户展示蓝图摘要，请求确认
3. 后续所有修改都以该文件为基础

---

### 阶段 3：用户确认

**目标**：确保用户认可设计方案后再开始绘制。

**展示内容**：
1. 模块列表及每个模块的元件清单
2. 关键网络连接关系
3. 芯片选型说明

**修改流程**：
- 用户提出修改意见 → 更新 `blueprint.json` → 重新展示 → 再次确认
- 循环直到用户确认

---

### 阶段 3.5：封装齐备性检查

**目标**：在开始绘制原理图之前，确保蓝图中所有元件的原理图符号（symbol）和PCB封装（footprint）在KiCAD中可用。避免在绘制过程中因缺少符号/封装而中断。

**检查范围**：
- 蓝图中每个元件的 `symbol` 字段（原理图符号）
- 蓝图中每个元件的 `footprint` 字段（PCB封装）

#### 三级解决策略

**第一级：查找 KiCAD 内置库（优先）**

KiCAD 自带丰富的元件库，大多数常用元件都能找到。使用 `search_symbol` 工具搜索：

```json
[
  {
    "tool_name": "search_symbol",
    "params": {
      "query": "ESP32",        // 搜索关键词
      "libraries": ["Device", "Connector", "MCU"]  // 可选：限定库范围
    }
  }
]
```

**搜索技巧**：
- 按芯片型号搜索（如 "DRV8301"、"INA240"）
- 按功能搜索（如 "MOSFET"、"opamp"、"regulator"）
- 按封装类型搜索（如 "QFP49"、"SOT-23"）

**第二级：查询 JLCPCB 库**

如果 KiCAD 内置库中没有，MCP 提供 JLCPCB 查询工具，可以查找嘉立创的元件库：

```json
[
  {
    "tool_name": "jlcpcb_search",    // JLCPCB 元件搜索
    "params": {
      "query": "ESP32-WROOM-32"
    }
  }
]
```

JLCPCB 库优势：
- 覆盖大量常用芯片和被动元件
- 封装经过实际生产验证
- 可直接用于 PCB 制造

**第三级：找相近符号修改（兜底策略）**

如果前两级都找不到，**不要从头绘制**，而是：

1. **找引脚数相近的现有符号**：例如需要 49 引脚 QFP，可找一个 48 或 52 引脚的 QFP 符号
2. **复制并修改**：
   - 使用 `clone_schematic_component` 复制现有符号
   - 用 `edit_schematic_component` 修改引脚数、引脚名称、封装
3. **常用替代符号**：
   - 复杂 IC → 用 `Device:Opamp_Dual` 或 `Connector:Conn_01xN` 作为占位符
   - 自定义模块 → 用 `Connector` 库中的连接器符号，引脚数灵活
   - 电源芯片 → 用 `Regulator` 库中的相近型号

**修改相近符号的示例**：

```json
[
  {
    "tool_name": "add_schematic_component",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "symbol": "Connector:Conn_01x49",    // 用49针连接器作为占位
      "reference": "U1",
      "value": "ESP32-WROOM-32",
      "footprint": "Module:ESP32-WROOM-32",
      "position": {"x": 100, "y": 100}
    }
  }
]
```

#### 封装检查报告

检查完成后，生成封装检查报告，格式如下：

```
封装齐备性检查报告
==================

✅ 可用元件 (30/34):
  - R1: Device:R / Resistor_SMD:R_0603_1608Metric
  - C1: Device:C / Capacitor_SMD:C_0603_1608Metric
  - ...

⚠️ 需要处理的元件 (4/34):
  1. U1 (ESP32-WROOM-32):
     - 符号: 内置库未找到 → 使用 Connector:Conn_01x49 作为占位
     - 封装: 需自定义 Module:ESP32-WROOM-32
     - 解决方案: 第三级策略 - 找相近符号修改
  
  2. U2 (DRV8301):
     - 符号: 内置库未找到 → 使用 Device:Opamp_Dual 作为占位
     - 封装: 需自定义
     - 解决方案: 第三级策略 - 找相近符号修改
  
  3. U3-U5 (INA240A2 ×3):
     - 符号: 内置库未找到 → 使用 Device:Opamp_Dual 作为占位
     - 封装: JLCPCB 库中有对应封装
     - 解决方案: 第二级策略 - 从 JLCPCB 获取封装
  
  4. J1 (USB-C):
     - 符号: JLCPCB 库中有
     - 封装: JLCPCB 库中有
     - 解决方案: 第二级策略 - 从 JLCPCB 获取

建议：先处理 ⚠️ 元件，确认方案后继续原理图绘制。
```

**用户确认**：展示封装检查报告，用户确认缺失元件的解决方案后，继续阶段4（原理图绘制）。

---

### 阶段 5：原理图绘制（模块聚类）

**目标**：基于蓝图 JSON，在 KiCAD 中创建原理图并按模块聚类放置元件。

#### 5.1 创建项目和原理图

```json
[
  {"tool_name": "create_project", "params": {"name": "ProjectName", "path": "/path/to/project"}},
  {"tool_name": "open_project", "params": {"filename": "/path/to/project/ProjectName.kicad_pro"}},
  {"tool_name": "create_schematic", "params": {"schematicPath": "/path/to/project/ProjectName.kicad_sch"}}
]
```

#### 5.2 模块聚类放置策略

**核心原则**：
1. **大芯片不动，小器件围绕**：`role: "master"` 的芯片（MCU、驱动IC等）位置固定，`role: "slave"` 的无源器件围绕其放置
2. **心中有线路**：放置时考虑连接关系，将需要连接的引脚放到相近位置
3. **调整无源器件**：通过旋转、镜像、移动无源器件来优化连线，不移动大芯片

**放置算法**：

```
对于每个模块:
  1. 先放置 master 元件（大芯片）在模块中心
  2. 对于每个 slave 元件:
     a. 查找它与 master 的连接关系
     b. 根据连接引脚方向，确定 slave 的放置位置
     c. 通过旋转/镜像调整 slave 方向，使引脚朝向 master
     d. 放置 slave 元件
```

**坐标计算示例**：

```python
# 模块中心 (100, 100)
# U1 (master) 放在 (100, 100)
# R1 需要连接 U1/Pin1 → 放在 U1 Pin1 附近
# 假设 U1 Pin1 在 (105, 95)
# R1 放在 (110, 95)，旋转 0°（引脚朝右）
# C1 需要连接 R1/Pin2 → 放在 R1 Pin2 附近
# C1 放在 (115, 95)，旋转 0°
```

#### 5.3 放置元件的工具调用

```json
[
  {
    "tool_name": "add_schematic_component",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "symbol": "Device:R",
      "reference": "R1",
      "value": "10k",
      "footprint": "Resistor_SMD:R_0603_1608Metric",
      "position": {"x": 110, "y": 95}
    }
  },
  {
    "tool_name": "rotate_schematic_component",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "reference": "R1",
      "angle": 90,
      "mirror": "y"
    }
  }
]
```

**关键工具参数**：

| 工具 | 关键参数 |
|------|----------|
| `add_schematic_component` | `schematicPath`, `symbol`, `reference`, `value`, `footprint`, `position{x,y}` |
| `move_schematic_component` | `schematicPath`, `reference`, `position{x,y}` |
| `rotate_schematic_component` | `schematicPath`, `reference`, `angle`(0/90/180/270), `mirror`(x/y) |

#### 5.4 放置顺序建议

1. **第一层**：所有 master 芯片（MCU、驱动IC、电源IC等）
2. **第二层**：与 master 直接连接的无源器件（去耦电容、上拉电阻等）
3. **第三层**：连接器、晶振、LED 等
4. **第四层**：电源符号、接地符号

---

### 阶段 6：原理图连线

**目标**：完成所有电气连接，确保网表正确。

#### 6.1 连线策略

**优先使用网络标签**，而非直接画线：
1. 对于跨模块连接 → 使用 `add_schematic_net_label`
2. 对于模块内短距离连接 → 使用 `add_schematic_connection`（自动连线）
3. 对于复杂连接 → 使用 `add_wire` 手动画线

#### 6.2 添加网络标签

```json
[
  {
    "tool_name": "add_schematic_net_label",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "netName": "3V3",
      "componentRef": "U1",
      "pinNumber": "5"
    }
  },
  {
    "tool_name": "add_schematic_net_label",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "netName": "GND",
      "componentRef": "C1",
      "pinNumber": "2"
    }
  }
]
```

**关键**：使用 `componentRef` + `pinNumber` 方式添加标签，标签会自动吸附到引脚位置，确保电气连接。

#### 6.3 添加导线连接

```json
[
  {
    "tool_name": "add_schematic_connection",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "sourceRef": "R1",
      "sourcePin": "1",
      "targetRef": "U1",
      "targetPin": "1"
    }
  }
]
```

#### 6.4 添加电源符号

```json
[
  {
    "tool_name": "add_schematic_component",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "symbol": "power_symbol:GND",
      "reference": "GND#1",
      "position": {"x": 115, "y": 105}
    }
  },
  {
    "tool_name": "add_schematic_component",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch",
      "symbol": "power_symbol:+3V3",
      "reference": "+3V3#1",
      "position": {"x": 105, "y": 85}
    }
  }
]
```

#### 6.5 验证连接

```json
[
  {"tool_name": "annotate_schematic", "params": {"schematicPath": "/path/to/project/ProjectName.kicad_sch"}},
  {"tool_name": "generate_netlist", "params": {"schematicPath": "/path/to/project/ProjectName.kicad_sch"}},
  {"tool_name": "list_schematic_nets", "params": {"schematicPath": "/path/to/project/ProjectName.kicad_sch"}}
]
```

**检查清单**：
- [ ] 所有元件位号唯一且连续
- [ ] 所有网络都有连接
- [ ] 没有未连接的引脚
- [ ] 电源网络正确

---

### 阶段 7：PCB 设计

**目标**：基于原理图生成 PCB 布局。

#### 7.1 原理图同步到 PCB

```json
[
  {
    "tool_name": "sync_schematic_to_board",
    "params": {
      "schematicPath": "/path/to/project/ProjectName.kicad_sch"
    }
  }
]
```

#### 7.2 PCB 布局

```json
[
  {"tool_name": "set_board_size", "params": {"width": 50, "height": 50, "unit": "mm"}},
  {"tool_name": "add_board_outline", "params": {"shape": "rectangle", "params": {"x": 0, "y": 0, "width": 50, "height": 50, "unit": "mm"}}},
  {"tool_name": "add_mounting_hole", "params": {"position": {"x": 5, "y": 5}, "diameter": 3}}
]
```

#### 7.3 布线

```json
[
  {
    "tool_name": "add_track",
    "params": {
      "start": {"x": 10, "y": 10},
      "end": {"x": 20, "y": 10},
      "width": 0.25,
      "layer": "F.Cu"
    }
  }
]
```

#### 7.4 导出

```json
[
  {"tool_name": "export_schematic_pdf", "params": {"path": "/path/to/project/schematic.pdf"}},
  {"tool_name": "export_schematic_svg", "params": {"path": "/path/to/project/schematic.svg"}},
  {"tool_name": "export_pcb_to_pdf", "params": {"path": "/path/to/project/pcb.pdf"}},
  {"tool_name": "export_pcb_to_svg", "params": {"path": "/path/to/project/pcb.svg"}},
  {"tool_name": "export_gerber", "params": {"path": "/path/to/project/gerber/"}},
  {"tool_name": "export_bom", "params": {"path": "/path/to/project/bom.csv"}},
  {"tool_name": "save_project", "params": {}}
]
```

---

## 工具分类参考

### 项目管理（5 个 Direct 工具）
- `create_project` - 创建新项目
- `open_project` - 打开现有项目
- `save_project` - 保存项目
- `get_project_info` - 获取项目信息
- `snapshot_project` - 保存项目快照（PDF + 步骤标签）

### 原理图设计（29 个工具）

#### 元件操作
- `add_schematic_component` - 添加元件到原理图（Direct）
- `delete_schematic_component` - 删除元件
- `edit_schematic_component` - 编辑元件属性
- `set_schematic_component_property` - 设置自定义属性
- `remove_schematic_component_property` - 删除自定义属性
- `get_schematic_component` - 获取元件信息
- `list_schematic_components` - 列出所有元件（Direct）
- `move_schematic_component` - 移动元件
- `rotate_schematic_component` - 旋转/镜像元件
- `annotate_schematic` - 自动标注位号（Direct）

#### 连线操作
- `add_wire` - 添加导线
- `delete_schematic_wire` - 删除导线
- `add_schematic_connection` - 连接两个引脚（自动连线）
- `add_schematic_net_label` - 添加网络标签（Direct）
- `delete_schematic_net_label` - 删除网络标签

#### 网络查询
- `get_schematic_pin_locations` - 获取引脚位置
- `get_net_connections` - 获取网络连接
- `list_schematic_nets` - 列出所有网络
- `list_schematic_wires` - 列出所有导线
- `list_schematic_labels` - 列出所有标签

#### 原理图创建与导出
- `create_schematic` - 创建原理图文件
- `get_schematic_view` - 获取原理图预览（PNG/SVG）
- `export_schematic_svg` - 导出 SVG
- `export_schematic_pdf` - 导出 PDF
- `generate_netlist` - 生成网表
- `sync_schematic_to_board` - 同步到 PCB（Direct）

### 板框操作（12 个工具）
- `set_board_size` - 设置板子尺寸
- `add_board_outline` - 添加板框轮廓
- `get_board_info` - 获取板子信息
- `add_mounting_hole` - 添加安装孔
- `set_board_thickness` - 设置板子厚度
- `set_board_layers` - 设置层数
- `add_keepout_zone` - 添加禁布区
- `add_copper_zone` - 添加铜区
- `import_svg_logo` - 导入 SVG Logo
- `add_text` - 添加文本
- `set_board_properties` - 设置板子属性
- `get_board_properties` - 获取板子属性

### 元件管理（16 个工具）
- `search_symbols` - 搜索符号库
- `add_component` - 添加元件
- `move_component` - 移动元件
- `rotate_component` - 旋转元件
- `delete_component` - 删除元件
- `get_component_info` - 获取元件信息
- `set_component_property` - 设置元件属性
- `search_footprints` - 搜索封装库
- `add_footprint` - 添加封装
- `get_footprint_info` - 获取封装信息
- `update_footprint` - 更新封装
- `import_footprint` - 导入封装
- `get_components_list` - 获取元件列表
- `set_component_value` - 设置元件值
- `flip_component` - 翻转元件
- `clone_component` - 克隆元件

### 布线（13 个工具）
- `add_track` - 添加走线
- `add_via` - 添加过孔
- `delete_track` - 删除走线
- `get_tracks` - 获取走线列表
- `route_interactive` - 交互式布线
- `set_track_width` - 设置走线宽度
- `add_net` - 添加网络
- `get_nets` - 获取网络列表
- `set_net_properties` - 设置网络属性
- `add_copper_pour` - 添加铺铜
- `remove_copper_pour` - 删除铺铜
- `update_copper_pour` - 更新铺铜
- `get_drc_errors` - 获取 DRC 错误

### 导出（8 个工具）
- `export_pcb_to_pdf` - 导出 PCB 为 PDF
- `export_pcb_to_svg` - 导出 PCB 为 SVG
- `export_board_to_svg` - 导出板框为 SVG
- `export_gerber` - 导出 Gerber 文件
- `export_drill_file` - 导出钻孔文件
- `export_bom` - 导出 BOM 表
- `export_step` - 导出 3D STEP 文件
- `export_pos` - 导出元件位置文件

### 设计规则/DRC（8 个工具）
- `set_design_rules` - 设置设计规则
- `run_drc` - 运行 DRC 检查
- `get_drc_errors` - 获取 DRC 错误
- `set_clearance` - 设置间距
- `set_track_width` - 设置走线宽度
- `set_via_size` - 设置过孔尺寸
- `get_design_rules` - 获取设计规则
- `validate_design` - 验证设计

### 库管理（8 个工具）
- `search_symbols` - 搜索符号
- `search_footprints` - 搜索封装
- `get_symbol_info` - 获取符号信息
- `get_footprint_info` - 获取封装信息
- `import_symbol` - 导入符号
- `import_footprint` - 导入封装
- `create_symbol` - 创建符号
- `create_footprint` - 创建封装

### 其他工具
- `check_kicad_ui` - 检查 KiCAD UI 是否运行
- `suggest_jlcpcb_alternatives` - 推荐 JLCPCB 替代元件
- `get_jlcpcb_pricing` - 获取 JLCPCB 价格
- `get_datasheet` - 获取数据手册

---

## 调试要点

### 原理图相关
1. **符号格式**：`symbol` 参数格式为 `Library:SymbolName`（如 `Device:R`、`power_symbol:GND`）
2. **schematicPath**：必须使用完整路径指向 `.kicad_sch` 文件
3. **position 参数**：是对象 `{x, y}`，单位 mm
4. **网络标签吸附**：使用 `componentRef` + `pinNumber` 添加标签，自动吸附到引脚
5. **引脚位置查询**：连线前先用 `get_schematic_pin_locations` 获取引脚坐标
6. **自动标注**：放置完所有元件后调用 `annotate_schematic` 自动分配位号

### 环境变量
7. **KICAD_PYTHON**：必须指向 KiCAD 自带的 Python
8. **PYTHONPATH**：必须设置 KiCAD 的 site-packages 路径

### 通用
9. **不需要打开 KiCAD GUI**：MCP Server 在后台运行
10. **批量操作**：使用 `kicad_mcp_batch` 保持会话状态
11. **元件搜索**：先用 `search_symbols` 查找可用元件
12. **单位**：默认使用 mm

---

## 常见错误与解决

### ModuleNotFoundError: No module named 'pcbnew'
- **原因**：使用了系统 Python 而非 KiCAD 自带的 Python
- **解决**：确保 `KICAD_PYTHON` 设置为 `C:\Program Files\KiCad\10.0\bin\python.exe`

### ConnectionRefused / MCP Server 无法启动
- **原因**：Node.js 未安装或 KiCAD-MCP-Server 未正确构建
- **解决**：运行 `npm install && npm run build` 重新构建

### open_project 失败
- **原因**：参数名错误，使用了 `path` 而非 `filename`
- **解决**：使用 `filename` 参数指向 `.kicad_pro` 文件

### add_schematic_component 失败
- **原因**：symbol 格式错误或符号库未加载
- **解决**：使用 `search_symbols` 确认符号名称，格式为 `Library:SymbolName`

### 网络标签不连接
- **原因**：标签位置与引脚位置不匹配
- **解决**：使用 `componentRef` + `pinNumber` 方式添加标签，自动吸附

### 元件搜索无结果
- **原因**：符号库未加载或搜索关键词不匹配
- **解决**：尝试使用通用关键词如 "LED"、"Resistor"、"Capacitor"

---

## 环境检查

在使用前，建议先检查环境是否就绪：

```bash
# 检查 KiCAD Python
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "import pcbnew; print(pcbnew.GetBuildVersion())"

# 检查 Node.js
node --version

# 检查 KiCAD-MCP-Server
where node
dir "KiCAD-MCP-Server\dist\index.js"
```

---

## 学习资源

- 官方仓库：https://github.com/mixelpixx/KiCAD-MCP-Server
- 工具清单：docs/TOOL_INVENTORY.md
- 原理图工具参考：docs/SCHEMATIC_TOOLS_REFERENCE.md
- MCP 协议规范：https://modelcontextprotocol.io/
