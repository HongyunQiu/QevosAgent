// UI i18n — reads window.QEVOS_LANG injected by server.js
(function () {
  const LANG = (window.QEVOS_LANG || 'zh').startsWith('zh') ? 'zh' : 'en';

  const STRINGS = {
    zh: {
      // index.html
      'nostop.title':          '持续对话模式：完成任务后不退出，等待下一个目标',
      'nostop.idle_banner':    '任务完成 — 请输入下一个目标',
      'runpill.kill_hint':     '长按中止',
      'ask_user.label':        '💬 Agent 向你提问',
      'ask_user.awaiting':     '⏳ 正在等你回答 — 在下方输入框作答',
      'openview.prompt':       '🔔 Agent 想展示:',
      'openview.open_btn':     '打开展示页',
      'openview.dismiss':      '关闭',
      'skill.default_content': '# SKILL: {name}\n\n适用领域：\n\n## 规范\n\n',
      'cron.default_content':  '---\nname: {name}\ncron: "0 9 * * *"   # 每天 9:00（分 时 日 月 周）\nenabled: true\non_conflict: skip   # skip（忙时跳过） | queue（忙时排队）\nskills: []\n# timezone: Asia/Shanghai\n---\n\n在这里写本次定时触发要执行的目标 / 提示词。\n',
      'concept.missing':       'memory_macro.md 尚不存在。',
      'concept.saved':         '✓ 宏观工作记忆已保存',
      'concept.default':       '# 宏观工作记忆\n\n## \n\n',
      'episodic.missing':      'memory_episodic.jsonl 尚不存在',
      'episodic.empty':        '暂无记录',
      'episodic.count':        '{n} 条',
      'episodic.invalid_json': '✗ 第 {line} 行 JSON 无效',
      'goal.filter.prefixes':  ['#', '【', '本次运行', '提示：', '你必须', '[RULES]', 'This run', 'Note: this run', 'Tools,', '[DOMAIN'],

      // view.html
      'view.waiting':          '等待 agent 调用 web_show…',
      'view.empty_p1':         'Agent 尚未发布任何内容',
      'view.empty_p2':         '调用 <code style="background:rgba(255,255,255,.08);padding:1px 5px;border-radius:3px;">web_show</code> 工具后内容将实时出现',
      'view.attach_title':     '附加图片',
      'view.input_placeholder':'向 Agent 发消息…',
      'view.send_btn':         '发送',
      'view.user_label':       '你',
      'view.img_alt':          '图片',
      'view.remove_title':     '移除',
      'view.chart_error':      '图表渲染失败:\n{e}\n\n原始数据:\n{data}',
      'view.empty_table':      '空表格',
      'view.web_user_prefix':  '[Web用户]',
      'view.send_failed':      '⚠ 发送失败: {msg}',
      'view.agent_offline':    'Agent 已下线，会话已结束',
      'view.agent_offline_msg':'⚫ Agent 已退出，本次会话结束。',
    },
    en: {
      // index.html
      'nostop.title':          'Continuous dialogue mode: stay running after task completion and wait for the next goal',
      'nostop.idle_banner':    'Task complete — enter the next goal',
      'runpill.kill_hint':     'Kill Task',
      'ask_user.label':        '💬 Agent is asking you',
      'ask_user.awaiting':     '⏳ Waiting for your answer — reply in the input box below',
      'openview.prompt':       '🔔 Agent wants to show:',
      'openview.open_btn':     'Open',
      'openview.dismiss':      'Dismiss',
      'skill.default_content': '# SKILL: {name}\n\nApplicable domain:\n\n## Rules\n\n',
      'cron.default_content':  '---\nname: {name}\ncron: "0 9 * * *"   # daily 9:00 (min hour day month weekday)\nenabled: true\non_conflict: skip   # skip (drop if busy) | queue (run after current task)\nskills: []\n# timezone: Asia/Shanghai\n---\n\nWrite the goal / prompt to run on each trigger here.\n',
      'concept.missing':       'memory_macro.md does not exist yet.',
      'concept.saved':         '✓ Macro memory saved',
      'concept.default':       '# Macro working memory\n\n## \n\n',
      'episodic.missing':      'memory_episodic.jsonl does not exist yet',
      'episodic.empty':        'No records yet',
      'episodic.count':        '{n} record(s)',
      'episodic.invalid_json': '✗ Line {line}: invalid JSON',
      'goal.filter.prefixes':  ['#', '【', '本次运行', '提示：', '你必须', '[RULES]', 'This run', 'Note: this run', 'Tools,', '[DOMAIN'],

      // view.html
      'view.waiting':          'Waiting for agent to call web_show…',
      'view.empty_p1':         'Agent has not published any content yet',
      'view.empty_p2':         'Content will appear here after <code style="background:rgba(255,255,255,.08);padding:1px 5px;border-radius:3px;">web_show</code> is called',
      'view.attach_title':     'Attach image',
      'view.input_placeholder':'Send a message to Agent…',
      'view.send_btn':         'Send',
      'view.user_label':       'You',
      'view.img_alt':          'image',
      'view.remove_title':     'Remove',
      'view.chart_error':      'Chart render failed:\n{e}\n\nRaw data:\n{data}',
      'view.empty_table':      'Empty table',
      'view.web_user_prefix':  '[Web user]',
      'view.send_failed':      '⚠ Send failed: {msg}',
      'view.agent_offline':    'Agent offline — session ended',
      'view.agent_offline_msg':'⚫ Agent disconnected — session ended.',
    },
  };

  const table = STRINGS[LANG] || STRINGS.zh;

  window.uiT = function (key, vars) {
    let s = table[key];
    if (s === undefined) s = (STRINGS.zh[key] !== undefined ? STRINGS.zh[key] : key);
    if (Array.isArray(s)) return s;
    if (vars) {
      s = s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] !== undefined ? vars[k] : '{' + k + '}'));
    }
    return s;
  };

  window.UI_LANG = LANG;
})();
