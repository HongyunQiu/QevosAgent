# SKILL: KiCAD MCP 电路设计

适用领域：PCB 设计、硬件原型开发、电子电路自动化。

## 概述

KiCAD MCP Server 是一个基于 Model Context Protocol (MCP) 的服务，允许 AI 助手通过标准协议控制 KiCAD 进行 PCB 设计自动化。提供 **124 个工具**（62 个核心工具 + 路由工具），涵盖项目管理、板框设置、元件放置、布线、原理图、导出等完整 PCB 设计流程。

## 环境要求

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
    {"tool_name": "set_board_size", "params": {"width": 50, "height": 50, "unit": "mm"}},
    {"tool_name": "save_project", "params": {}}
  ]
}
```

## 核心工作流

### 1. 创建项目

```json
{"tool_name": "create_project", "params": {"name": "ProjectName", "path": "/path/to/dir"}}
```

### 2. 打开项目

```json
{"tool_name": "open_project", "params": {"filename": "/path/to/ProjectName.kicad_pro"}}
```

**注意**：`open_project` 使用 `filename` 参数（不是 `path`），指向 `.kicad_pro` 或 `.kicad_pcb` 文件。

### 3. 设置板子尺寸

```json
{"tool_name": "set_board_size", "params": {"width": 50, "height": 50, "unit": "mm"}}
```

### 4. 添加板子轮廓

```json
{
  "tool_name": "add_board_outline",
  "params": {
    "shape": "rectangle",
    "params": {"x": 0, "y": 0, "width": 50, "height": 50, "unit": "mm"}
  }
}
```

支持形状：`rectangle`、`circle`、`polygon`、`rounded_rectangle`

### 5. 搜索并放置元件

```json
{"tool_name": "search_symbols", "params": {"query": "LED"}}
{"tool_name": "add_component", "params": {"symbol": "LED", "position": {"x": 10, "y": 10}}}
```

### 6. 添加安装孔

```json
{"tool_name": "add_mounting_hole", "params": {"position": {"x": 5, "y": 5}, "diameter": 3}}
```

### 7. 布线

```json
{
  "tool_name": "add_track",
  "params": {
    "start": {"x": 10, "y": 10},
    "end": {"x": 20, "y": 10},
    "width": 0.25,
    "layer": "F.Cu"
  }
}
```

### 8. 导出文件

```json
{"tool_name": "export_pcb_to_pdf", "params": {"path": "/path/to/output.pdf"}}
{"tool_name": "export_pcb_to_svg", "params": {"path": "/path/to/output.svg"}}
{"tool_name": "export_board_to_svg", "params": {"path": "/path/to/output.svg"}}
```

### 9. 保存项目

```json
{"tool_name": "save_project", "params": {}}
```

## 工具分类参考

### 项目管理（5 个 Direct 工具）
- `create_project` - 创建新项目
- `open_project` - 打开现有项目
- `save_project` - 保存项目
- `get_project_info` - 获取项目信息
- `snapshot_project` - 保存项目快照（PDF + 步骤标签）

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

### 原理图（27 个工具）
- `create_schematic_sheet` - 创建原理图纸
- `add_symbol` - 添加符号
- `wire_symbols` - 连线
- `add_wire` - 添加导线
- `add_label` - 添加标签
- `add_hierarchial_pin` - 添加层次引脚
- `schematic_to_board` - 原理图同步到 PCB
- `annotate_schematic` - 标注原理图
- 等...

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
- 等...

## 示例：LED 电路设计

完整的 LED + 电阻电路设计流程：

```json
[
  {"tool_name": "create_project", "params": {"name": "LED_Circuit", "path": "/path/to/led_circuit"}},
  {"tool_name": "open_project", "params": {"filename": "/path/to/led_circuit/LED_Circuit.kicad_pro"}},
  {"tool_name": "set_board_size", "params": {"width": 50, "height": 50, "unit": "mm"}},
  {"tool_name": "add_board_outline", "params": {"shape": "rectangle", "params": {"x": 0, "y": 0, "width": 50, "height": 50, "unit": "mm"}}},
  {"tool_name": "add_mounting_hole", "params": {"position": {"x": 5, "y": 5}, "diameter": 3}},
  {"tool_name": "add_mounting_hole", "params": {"position": {"x": 45, "y": 5}, "diameter": 3}},
  {"tool_name": "add_mounting_hole", "params": {"position": {"x": 5, "y": 45}, "diameter": 3}},
  {"tool_name": "add_mounting_hole", "params": {"position": {"x": 45, "y": 45}, "diameter": 3}},
  {"tool_name": "search_symbols", "params": {"query": "LED"}},
  {"tool_name": "add_component", "params": {"symbol": "LED", "position": {"x": 25, "y": 25}}},
  {"tool_name": "search_symbols", "params": {"query": "Resistor"}},
  {"tool_name": "add_component", "params": {"symbol": "Resistor", "position": {"x": 15, "y": 25}}},
  {"tool_name": "add_track", "params": {"start": {"x": 15, "y": 25}, "end": {"x": 25, "y": 25}, "width": 0.25, "layer": "F.Cu"}},
  {"tool_name": "export_pcb_to_pdf", "params": {"path": "/path/to/led_circuit/LED_Circuit.pdf"}},
  {"tool_name": "export_pcb_to_svg", "params": {"path": "/path/to/led_circuit/LED_Circuit.svg"}},
  {"tool_name": "save_project", "params": {}}
]
```

## 调试要点

1. **环境变量**：`KICAD_PYTHON` 必须指向 KiCAD 自带的 Python，否则导入 KiCAD 模块会失败
2. **open_project 参数**：使用 `filename` 而不是 `path`，指向 `.kicad_pro` 文件
3. **set_board_size 参数**：需要 `width`、`height`、`unit` 三个参数
4. **add_board_outline 参数**：需要 `shape` 和 `params`（包含 x, y, width, height, unit）
5. **不需要打开 KiCAD GUI**：MCP Server 在后台运行，无需 GUI
6. **批量操作**：使用 `kicad_mcp_batch` 可以保持会话状态，避免重复打开项目
7. **元件搜索**：先用 `search_symbols` 查找可用元件，再用 `add_component` 放置
8. **单位**：默认使用 mm，可在参数中指定 `unit: "mm"`

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

### set_board_size 失败
- **原因**：缺少 `unit` 参数
- **解决**：必须同时提供 `width`、`height`、`unit` 三个参数

### add_board_outline 失败
- **原因**：`params` 中缺少必要字段
- **解决**：`params` 必须包含 `x`, `y`, `width`, `height`, `unit`

### KiCAD 进程崩溃
- **原因**：PYTHONPATH 未正确设置
- **解决**：设置 `PYTHONPATH=C:\Program Files\KiCad\10.0\bin\Lib\site-packages`

### 元件搜索无结果
- **原因**：符号库未加载或搜索关键词不匹配
- **解决**：尝试使用通用关键词如 "LED"、"Resistor"、"Capacitor"

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

## 学习资源

- 官方仓库：https://github.com/mixelpixx/KiCAD-MCP-Server
- 工具清单：docs/TOOL_INVENTORY.md
- 原理图工具参考：docs/SCHEMATIC_TOOLS_REFERENCE.md
- MCP 协议规范：https://modelcontextprotocol.io/
