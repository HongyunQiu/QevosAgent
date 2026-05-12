'use strict';

/**
 * Minimal i18n for the Electron desktop process — UI strings only.
 *
 * Language detection order:
 *   1. QEVOS_LANG env var (zh / en)
 *   2. System locale via Intl / LANG env
 *   3. Default: zh
 */

function detectLang() {
  const override = process.env.QEVOS_LANG || '';
  if (override) return override.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  try {
    const sys = Intl.DateTimeFormat().resolvedOptions().locale || process.env.LANG || '';
    return sys.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  } catch {
    return 'zh';
  }
}

const LANG = detectLang();

const _STRINGS = {
  zh: {
    'menu.about': '关于 QevosAgent',
    'menu.quit':  '退出',

    'app.invalid_url':    '无效的 API 地址格式',
    'app.timeout':        '连接超时（8 秒）',
    'app.load_error':     '无法加载 Dashboard：{error}',
    'app.server_not_ready':
      'Dashboard 未能在端口 {port} 启动\n（已等待 {secs} 秒）',

    // Internal protocol markers — must mirror agent/i18n.py marker.* keys
    'marker.tool_prefix':    '[工具: {name}]',
    'marker.tool_success':   '执行成功',
    'marker.tool_failure':   '执行失败',
    'marker.advisor_prefix': '[高级指导员',
    'marker.user_inject':    '[用户干预注入]',
    'marker.user_info':      '[用户补充信息]',
    'marker.goal_marker':    '请完成以下目标：',
    'marker.system_prefix':  '[系统]',
    'marker.system_cmd':     '[系统指令]',
  },
  en: {
    'menu.about': 'About QevosAgent',
    'menu.quit':  'Quit',

    'app.invalid_url':    'Invalid API URL format',
    'app.timeout':        'Connection timed out (8 s)',
    'app.load_error':     'Failed to load Dashboard: {error}',
    'app.server_not_ready':
      'Dashboard failed to start on port {port}\n(waited {secs} seconds)',
  },
};

/**
 * Return the localised string for key, interpolating {placeholders}.
 * Falls back to zh, then to the key itself.
 */
function t(key, vars = {}) {
  const table = _STRINGS[LANG] || _STRINGS.zh;
  let s = table[key] ?? (_STRINGS.zh[key] ?? key);
  return s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
}

module.exports = { LANG, t };
