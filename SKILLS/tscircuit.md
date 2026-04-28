# SKILL: tscircuit 电路设计

适用领域：电子电路设计、PCB 布局、硬件原型开发。

## 概述

tscircuit 是一个用 TypeScript/React JSX 语法来设计电子电路的工具，可自动生成原理图、PCB 布局、3D 模型和制造文件。

## 环境要求

- Node.js >= 18
- npm
- tscircuit CLI：`npm install -g tscircuit`

## 项目初始化

```bash
tsci init <项目名> -y
cd <项目名>
npm install
```

## 开发工作流

### 1. 编写电路代码

电路代码写在 `src/Board.tsx`，使用 JSX 语法：

```tsx
export default () => (
  <board width="10mm" height="10mm">
    <resistor name="R1" resistance="1k" footprint="0402" pcbX={0} pcbY={0} />
    <led name="LED1" color="red" footprint="0603" />
    <trace from=".R1 > .pin1" to=".LED1 > .pos" />
    <trace from=".LED1 > .neg" to="net.GND" />
  </board>
)
```

### 2. 实时预览

```bash
tsci dev
# 打开 http://localhost:3020 查看原理图和 PCB
```

### 3. 构建导出

```bash
tsci build
# 生成 build/ 目录，包含 circuit.json 和制造文件
```

## 完整示例：ESP32-WROOM-32 最小系统

以下是一个包含复位电路、GPIO0启动配置、UART串口和电源去耦的ESP32最小系统：

```tsx
import { PushButton } from "@tsci/seveibar.push-button"

export default () => (
  <board width="120mm" height="80mm">
    {/* 网络定义 */}
    <net name="GND" />
    <net name="VCC_3V3" />
    <net name="TX" />
    <net name="RX" />

    {/* ESP32-WROOM-32 模块 */}
    <chip
      name="U1"
      pinLabels={{
        pin1: ["VDDA"],
        pin3: ["VDD3P3"],
        pin14: ["GPIO25"],
        pin23: ["GPIO0"],
        pin40: ["RX"],
        pin41: ["TX"],
        pin49: ["GND"]
      }}
      footprint="qfp49"
      manufacturerPartNumber="ESP32-WROOM-32"
      pcbX={0}
      pcbY={0}
    />

    {/* EN 引脚复位电路 */}
    <resistor name="R_EN_PULL" resistance="10k" footprint="0402" pcbX={-15} pcbY={10} />
    <trace from=".U1 > .GPIO25" to=".R_EN_PULL > .pin1" />
    <trace from=".R_EN_PULL > .pin2" to="net.VCC_3V3" />

    {/* 复位按钮 */}
    <PushButton name="BTN_RESET" pcbX={-15} pcbY={20} />
    <trace from=".U1 > .GPIO25" to=".BTN_RESET > .pin1" />
    <trace from=".BTN_RESET > .pin2" to="net.GND" />

    {/* GPIO0 启动配置（上拉=正常启动，下拉=下载模式）*/}
    <resistor name="R_GPIO0_PULL" resistance="10k" footprint="0402" pcbX={15} pcbY={10} />
    <trace from=".U1 > .GPIO0" to=".R_GPIO0_PULL > .pin1" />
    <trace from=".R_GPIO0_PULL > .pin2" to="net.VCC_3V3" />

    {/* GPIO0 下载模式按钮 */}
    <PushButton name="BTN_BOOT" pcbX={15} pcbY={20} />
    <trace from=".U1 > .GPIO0" to=".BTN_BOOT > .pin1" />
    <trace from=".BTN_BOOT > .pin2" to="net.GND" />

    {/* UART 串口 */}
    <resistor name="R_TX" resistance="0" footprint="0402" pcbX={20} pcbY={-5} />
    <resistor name="R_RX" resistance="0" footprint="0402" pcbX={20} pcbY={-15} />
    <trace from=".U1 > .TX" to=".R_TX > .pin1" />
    <trace from=".R_TX > .pin2" to="net.TX" />
    <trace from=".U1 > .RX" to=".R_RX > .pin1" />
    <trace from=".R_RX > .pin2" to="net.RX" />

    {/* 电源去耦电容 */}
    <capacitor name="C_VCC1" capacitance="10uF" footprint="0805" pcbX={-25} pcbY={-10} />
    <capacitor name="C_VCC2" capacitance="0.1uF" footprint="0402" pcbX={-20} pcbY={-10} />
    <capacitor name="C_VCC3" capacitance="0.1uF" footprint="0402" pcbX={-15} pcbY={-10} />
    <trace from=".C_VCC1 > .pin1" to="net.VCC_3V3" />
    <trace from=".C_VCC1 > .pin2" to="net.GND" />
    <trace from=".C_VCC2 > .pin1" to="net.VCC_3V3" />
    <trace from=".C_VCC2 > .pin2" to="net.GND" />
    <trace from=".C_VCC3 > .pin1" to="net.VCC_3V3" />
    <trace from=".C_VCC3 > .pin2" to="net.GND" />

    {/* GND 和电源连接 */}
    <trace from=".U1 > .GND" to="net.GND" />
    <trace from=".U1 > .VDD3P3" to="net.VCC_3V3" />
  </board>
)
```

**设计要点：**
- EN引脚（GPIO25）：上拉电阻+复位按钮+去耦电容
- GPIO0：上拉电阻（正常启动）+按钮（下载模式）
- UART：通过0Ω电阻连接，便于调试时断开
- 电源：10uF+0.1uF+0.1uF三级去耦

---

## 核心语法规范

### 常用元件

| 元件 | JSX 标签 | 关键属性 |
|------|----------|----------|
| 电阻 | `<resistor>` | name, resistance, footprint |
| 电容 | `<capacitor>` | name, capacitance, footprint |
| LED | `<led>` | name, color, footprint |
| 按钮 | `<pushbutton>` | name, footprint |
| 电感 | `<inductor>` | name, inductance, footprint |
| 芯片 | `<chip>` | name, pinLabels, footprint, manufacturerPartNumber |

### Chip 组件（复杂封装）

对于多引脚芯片（如 MCU、传感器），使用 `<chip>` 组件配合 `pinLabels` 精确映射引脚：

```tsx
<chip
  name="U1"
  pinLabels={{
    pin1: ["VDDA"],
    pin3: ["VDD3P3"],
    pin23: ["GPIO0"],
    pin40: ["RX"],
    pin41: ["TX"],
    pin49: ["GND"]
  }}
  footprint="qfp49"
  manufacturerPartNumber="ESP32-WROOM-32"
  pcbX={0}
  pcbY={0}
/>
```

**关键要点：**
- `pinLabels` 只定义需要连接的引脚，未定义的引脚不会报错
- 引脚名称用于 trace 连接：`.U1 > .GPIO0`
- 支持 QFP、QFN、BGA 等复杂封装
- `manufacturerPartNumber` 用于 BOM 生成

**引脚连接示例：**
```tsx
<trace from=".U1 > .GPIO0" to=".R1 > .pin1" />
<trace from=".U1 > .GND" to="net.GND" />
<trace from=".U1 > .VDD3P3" to="net.VCC_3V3" />
```

### 连接语法

```tsx
{/* 元件间连接 */}
<trace from=".R1 > .pin1" to=".C1 > .pin1" />

{/* 连接到网络 */}
<trace from=".LED > .neg" to="net.GND" />
```

**引脚命名规则：**
- 电阻/电容：`.pin1`, `.pin2`
- LED：`.pos` (正极), `.neg` (负极)
- 按钮：`.pin1` ~ `.pin4`

### 网络定义

```tsx
<net name="GND" />
<net name="VCC" />
```

### 位置控制

- `pcbX`/`pcbY`：PCB 坐标（mm）
- `schX`/`schY`：原理图坐标
- `pcbRotation`：旋转角度（如 `"90deg"`）

### 外部组件

从 npm 安装并导入第三方组件：

```tsx
// 安装：npm install @tsci/seveibar.push-button
import { PushButton } from "@tsci/seveibar.push-button"

// 使用 PushButton（推荐，封装正确）
<PushButton
  name="BTN_RESET"
  pcbX={-15}
  pcbY={20}
/>
<trace from=".U1 > .GPIO25" to=".BTN_RESET > .pin1" />
<trace from=".BTN_RESET > .pin2" to="net.GND" />
```

**常用外部组件：**
- `@tsci/seveibar.push-button`：SMD 按钮开关
- `@tsci/seveibar.smd-usb-c`：USB-C 接口
- 更多组件：https://www.npmjs.com/search?q=%40tsci

**注意事项：**
- 原生 `<pushbutton>` 的封装解析可能失败（如 `button_smd_6x6`），建议使用 `@tsci/seveibar.push-button`
- 外部组件通过 `connections` 属性或 trace 连接

## Windows 兼容性

### 已知限制

| 命令 | Windows 支持 | 说明 |
|------|-------------|------|
| `tsci init` | 支持 | 正常 |
| `tsci dev` | 支持 | 正常，用于预览调试 |
| `tsci build` | 不支持 | ESM URL scheme 错误 |
| `tsci snapshot` | 不支持 | ESM URL scheme 错误 |
| `tsci clone` | 支持 | 正常 |

### 解决方案

1. **在线编辑器**（推荐）：https://tscircuit.com/editor
2. **WSL2**：在 WSL 中运行构建命令
3. **Docker**：使用 node:20 镜像运行构建

## 调试要点

1. 元件不显示：检查是否在 `<board>` 内、名称是否唯一
2. 连接错误：确认引脚名称（`.pin1`/`.pos`/`.neg`）
3. 封装找不到：使用标准封装名（0402/0603/0805）
4. 构建失败：切换到 WSL 或使用在线编辑器
5. **Trace 源端口缺失坐标**：部分引脚（如 chip 的未定义引脚）可能没有坐标，导致 trace 被跳过，需在 `pinLabels` 中定义所有需要连接的引脚
6. **按钮封装解析失败**：原生 `<pushbutton>` 的封装如 `button_smd_6x6` 可能解析失败，改用 `@tsci/seveibar.push-button` 的 `<PushButton>` 组件
7. **引脚未指定警告**："All pins on U1 are underspecified" 是警告而非错误，不影响构建，只需定义需要连接的引脚即可
8. **未连接的 Trace**："is not connected (it has no PCB trace)" 表示自动布线失败，可手动调整元件位置或忽略（原理图仍正确）

## 工具偏好

- 编写电路代码：`write_file` 创建 `.tsx` 文件
- 启动预览：`shell` 执行 `tsci dev`
- 展示结果：`web_show` 展示预览页面
- 构建导出：在 WSL/Linux 环境中执行 `tsci build`

## 学习资源

- 官方文档：https://docs.tscircuit.com/
- 在线编辑器：https://tscircuit.com/editor
- GitHub：https://github.com/tscircuit/tscircuit
