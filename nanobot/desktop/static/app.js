'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('catgirlmentor-session') || '';
let snapshot, populated = false, activeView = 'overview', following = true, rawLogs = '', pending = false;
let modelsRequest = 0;
const states = {
  unconfigured: ['待配置', '让助手认识你的模型', '添加模型连接，几步就能开始使用。', '开始配置'],
  stopped: ['已停止', '准备好，随时出发。', '启动后台后，即可打开聊天界面。', '启动后台'],
  starting: ['启动中', '助手正在准备中', '正在加载配置并检查后台是否就绪。', '启动中…'],
  running: ['运行中', '助手已就绪。', '你可以开始聊天，或让任务继续在后台运行。', '打开聊天界面'],
  stopping: ['停止中', '正在结束后台任务', '管理页面会保持打开，请稍候。', '停止中…'],
  failed: ['启动失败', '需要处理一点小问题', '查看下面的错误提示，调整配置后再试。', '重试启动']
};
function notice(message, error = false) { $('notice').textContent = message; $('notice').hidden = !message; $('notice').className = error ? 'error' : ''; }
async function api(path, data) {
  const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST', headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  let result; try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) throw new Error(response.status === 401 ? '管理会话已失效，请从托盘或桌面图标重新打开。' : result.error || '操作失败，请重试。');
  return result;
}
function view(name) {
  activeView = name;
  document.querySelectorAll('.view').forEach(el => el.hidden = el.id !== name);
  document.querySelectorAll('nav button').forEach(el => el.setAttribute('aria-current', el.dataset.view === name ? 'page' : 'false'));
  if (name === 'logs') refreshLogs();
  if (name === 'settings') loadStartup();
}
document.querySelectorAll('[data-view]').forEach(el => el.onclick = () => view(el.dataset.view));
document.querySelector('.brand').onclick = event => { event.preventDefault(); view('overview'); };
function render(data) {
  snapshot = data;
  const info = states[data.state] || ['正在检查', '正在检查后台状态', '请稍候。', '正在检查…'];
  $('state').textContent = info[0]; $('state').className = 'badge ' + data.state;
  $('state-title').textContent = info[1]; $('state-description').textContent = info[2];
  $('primary').textContent = info[3]; $('primary').disabled = pending || ['starting', 'stopping'].includes(data.state);
  $('runtime-error').textContent = data.error; $('runtime-error').hidden = !data.error;
  $('restart').hidden = $('stop').hidden = !data.pid || data.source !== 'application' || ['starting', 'stopping'].includes(data.state);
  $('version').textContent = 'v' + data.version; $('settings-version').textContent = data.version;
  $('source').textContent = {application: '本应用', external: '外部 CLI（在原入口管理）', none: '未运行'}[data.source];
  $('config-path').textContent = data.configPath; $('manager-url').textContent = location.origin;
  $('chat-url').textContent = data.chatUrl || '配置后显示';
  $('existing-choice').hidden = !data.existingAvailable || data.useExisting;
  $('use-independent').hidden = !data.useExisting;
  $('settings-use-existing').hidden = !data.existingAvailable || data.useExisting;
  $('data-source-options').hidden = !data.useExisting && !data.existingAvailable;
  $('data-source-description').textContent = data.useExisting ? '当前使用：以前的 nanobot 数据，继续沿用原来的模型配置和会话。' : '当前使用：本应用的数据，模型配置和会话单独保存在此目录中。';
  $('model-summary').textContent = data.model.model || '设置你的模型与密钥 →';
  const select = $('provider');
  for (const provider of data.providers) if (![...select.options].some(option => option.value === provider)) select.add(new Option(provider, provider));
  if (!populated && Object.keys(data.model).length) {
    if (data.model.provider && ![...select.options].some(option => option.value === data.model.provider)) select.add(new Option(data.model.provider, data.model.provider));
    select.value = data.model.provider || 'deepseek'; $('model-name').value = data.model.model || '';
    $('api-base').value = data.model.apiBase || ''; $('advanced').open = !!data.model.apiBase;
    $('key-hint').textContent = data.model.hasKey ? '已保存密钥。留空表示保持原值。' : '密钥只保存在你的电脑上。';
    $('preset-hint').hidden = !data.model.preset;
    populated = true;
  }
}
async function refresh() { try { render(await api('status')); } catch (err) { notice(err.message, true); $('primary').disabled = true; $('state').textContent = '连接中断'; } }
async function action(name, data = {}) {
  pending = true; document.querySelectorAll('[data-action]').forEach(el => el.disabled = true);
  try { await api(name, data); notice(''); await refresh(); return true; }
  catch (err) { notice(err.message, true); return false; }
  finally { pending = false; document.querySelectorAll('[data-action]').forEach(el => el.disabled = false); if (snapshot) render(snapshot); }
}
document.querySelectorAll('[data-action]').forEach(el => el.onclick = () => action(el.dataset.action));
$('primary').onclick = async () => {
  if (!snapshot) return;
  if (snapshot.state === 'unconfigured') return view('model');
  if (snapshot.state === 'running') {
    const tab = window.open('about:blank', '_blank');
    try { const result = await api('chat', {}); if (tab) { tab.opener = null; tab.location.replace(result.url); } else location.assign(result.url); } catch (err) { if (tab) tab.close(); notice(err.message, true); }
  } else await action('start');
};
async function saveModel(test) {
  if (!$('model-form').reportValidity()) return;
  const buttons = $('model-form').querySelectorAll('button'); buttons.forEach(el => el.disabled = true);
  $('model-result').textContent = test ? '正在保存并测试连接…' : '正在保存…';
  try {
    await api('config', Object.fromEntries(new FormData($('model-form')))); $('api-key').value = '';
    populated = false; await refresh();
    $('model-result').textContent = '配置已保存。运行中的后台需重启后使用新配置。';
    if (test) { await api('test', {}); $('model-result').textContent = '连接成功，可以回到概览启动后台。'; }
  } catch (err) { $('model-result').textContent = err.message; }
  finally { buttons.forEach(el => el.disabled = false); }
}
$('model-form').onsubmit = event => { event.preventDefault(); saveModel(false); };
$('test-connection').onclick = () => saveModel(true);
function clearModels() {
  modelsRequest++; $('model-options').replaceChildren(); $('model-options').hidden = true; $('fetch-models').disabled = false;
  $('models-result').textContent = '填写连接信息后，点击获取模型。无需先保存配置。';
}
$('fetch-models').onclick = async () => {
  const request = ++modelsRequest;
  $('fetch-models').disabled = true; $('model-options').replaceChildren();
  $('models-result').textContent = '正在获取可选模型…';
  try {
    const result = await api('models', Object.fromEntries(new FormData($('model-form'))));
    if (request !== modelsRequest) return;
    $('model-options').replaceChildren(new Option('选择一个模型，或在上方手动填写', ''), ...result.models.map(name => new Option(name, name)));
    $('model-options').hidden = !result.models.length;
    $('models-result').textContent = result.models.length ? `${result.message}（${result.models.length} 个，从下拉列表选择）` : result.message;
  } catch (err) { if (request === modelsRequest) $('models-result').textContent = err.message; }
  finally { if (request === modelsRequest) $('fetch-models').disabled = false; }
};
$('model-options').onchange = () => { if ($('model-options').value) $('model-name').value = $('model-options').value; };
$('provider').onchange = () => {
  clearModels();
  const saved = snapshot?.connections?.[$('provider').value];
  $('api-key').value = ''; $('api-base').value = saved?.apiBase || '';
  $('advanced').open = $('provider').value === 'custom' || !!saved?.apiBase;
  $('key-hint').textContent = saved?.hasKey ? '此服务商已保存密钥。留空表示保持原值。' : '密钥只保存在你的电脑上。';
};
$('api-key').oninput = $('api-base').oninput = clearModels;
function resetModelForm() {
  populated = false; $('model-form').reset(); $('api-key').value = '';
  $('advanced').open = false; $('preset-hint').hidden = true;
  $('key-hint').textContent = '密钥只保存在你的电脑上。'; $('model-result').textContent = '';
  clearModels();
}
async function selectConfig(useExisting) {
  clearModels();
  if (await action('select-config', {useExisting})) { resetModelForm(); await refresh(); }
}
$('use-existing').onclick = () => selectConfig(true); $('use-independent').onclick = () => selectConfig(false);
$('settings-use-existing').onclick = () => selectConfig(true);
function renderLogs() { const search = $('log-search').value.toLowerCase(), level = $('log-level').value; $('log-output').textContent = rawLogs.split('\n').filter(line => line.toLowerCase().includes(search) && (!level || line.includes(level))).join('\n'); if (following) $('log-output').scrollTop = $('log-output').scrollHeight; }
async function refreshLogs() { try { rawLogs = (await api('logs')).text; renderLogs(); } catch (err) { notice(err.message, true); } }
$('log-search').oninput = $('log-level').onchange = renderLogs;
$('follow').onclick = () => { following = !following; $('follow').textContent = following ? '暂停跟随' : '继续跟随'; if (following) refreshLogs(); };
async function copy(text) { try { await navigator.clipboard.writeText(text); notice('已复制。'); } catch { notice('复制失败，请手动选择文本复制。', true); } }
$('copy-logs').onclick = () => copy($('log-output').textContent); $('copy-path').onclick = () => copy(snapshot.configPath);
async function loadStartup() { try { $('startup').checked = (await api('startup')).enabled; } catch (err) { $('startup-result').textContent = err.message; } }
$('startup').onchange = async () => { const wanted = $('startup').checked; $('startup').disabled = true; try { await api('startup', {enabled: wanted}); $('startup-result').textContent = wanted ? '已添加登录自启动。' : '已关闭登录自启动。'; } catch (err) { $('startup').checked = !wanted; $('startup-result').textContent = err.message; } finally { $('startup').disabled = false; await loadStartup(); } };
$('reset-config').onclick = async () => { if (confirm('将当前配置备份后重置。会话和其他数据会保留，确定继续？')) { clearModels(); if (await action('reset-config', {confirm: true})) { resetModelForm(); view('model'); } } };
$('exit').onclick = async () => { if (confirm('退出 CatgirlMentor 并停止由本应用启动的后台？')) { try { await api('exit', {}); document.body.textContent = 'CatgirlMentor 已退出，可以关闭此页面。'; clearInterval(timer); } catch (err) { notice(err.message, true); } } };
let timer;
(async () => {
  try {
    const ticket = location.hash.slice(1); history.replaceState(null, '', location.pathname);
    if (ticket) { const result = await api('session', {token: ticket}); token = result.token; sessionStorage.setItem('catgirlmentor-session', token); }
    await refresh();
    timer = setInterval(() => { refresh(); if (activeView === 'logs' && following) refreshLogs(); }, 2500);
  } catch (err) { notice(err.message, true); }
})();
