import { useEffect, useRef, useState } from 'react';

const settingsFields = [
  ['jar_dir', 'JAR包目录', 'text', '/opt/app/jars'],
  ['script_dir', '脚本目录', 'text', '/opt/app/scripts'],
  ['backup_dir', '备份目录', 'text', '/opt/app/backup'],
  ['log_dir', '日志目录', 'text', '/home/app/applog/'],
  ['jar_prefix', 'JAR前缀', 'text', 'land-'],
  ['jar_suffix', 'JAR后缀', 'text', '-biz.jar'],
  ['script_prefix', '脚本前缀', 'text', 'shell-'],
  ['script_suffix', '脚本后缀', 'text', '.sh'],
  ['start_wait', '启动等待(秒)', 'number', '10'],
  ['deploy_interval', '部署间隔(秒)', 'number', '30']
];

function getJarName(jar) {
  return typeof jar === 'string' ? jar : jar.name;
}

function normalizeJarName(name) {
  return String(name || '').replace(/\s*\(\d+\)(?=\.jar$)/i, '');
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function getLogSaveDirKey(jarName) {
  return `deploy-log-save-dir:${jarName || 'default'}`;
}

async function readActionResult(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.success === false) {
    throw new Error(data.error || `请求失败 (${res.status})`);
  }
  return data;
}

function Modal({ title, icon, children, footer, onClose }) {
  return (
    <div className="react-modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal-dialog modal-lg react-modal">
        <div className="modal-content">
          <div className="modal-header">
            <h5 className="modal-title"><i className={`bi ${icon} me-2`} />{title}</h5>
            <button type="button" className="btn-close" onClick={onClose} />
          </div>
          <div className="modal-body">{children}</div>
          <div className="modal-footer">{footer}</div>
        </div>
      </div>
    </div>
  );
}

function AuthFields({ prefix, title, settings, setSettings }) {
  const useKey = String(settings[`${prefix}_use_private_key`] || '') === 'true';
  const update = (key, value) => setSettings((data) => ({ ...data, [key]: value }));
  return (
    <section className="settings-auth-section">
      <strong>{title}</strong>
      <div className="settings-grid">
        <label className="form-field"><span>用户名</span><input className="form-control" value={settings[`${prefix}_user`] || ''} onChange={(e) => update(`${prefix}_user`, e.target.value)} /></label>
        <label className="form-field form-check form-switch"><span>使用私钥认证</span><input className="form-check-input" type="checkbox" checked={useKey} onChange={(e) => update(`${prefix}_use_private_key`, String(e.target.checked))} /></label>
        {useKey ? <><label className="form-field"><span>私钥路径</span><input className="form-control" value={settings[`${prefix}_key_path`] || ''} onChange={(e) => update(`${prefix}_key_path`, e.target.value)} /></label><label className="form-field"><span>私钥口令（可选）</span><input className="form-control" type="password" value={settings[`${prefix}_key_passphrase`] || ''} onChange={(e) => update(`${prefix}_key_passphrase`, e.target.value)} /></label></> : <label className="form-field"><span>密码</span><input className="form-control" type="password" value={settings[prefix === 'server' ? 'password' : 'jump_password'] || ''} onChange={(e) => update(prefix === 'server' ? 'password' : 'jump_password', e.target.value)} /></label>}
      </div>
    </section>
  );
}

export default function App() {
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem('darkMode');
    return saved === '1' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches);
  });
  const [activeTab, setActiveTab] = useState('deploy');
  const [servers, setServers] = useState([]);
  const [jars, setJars] = useState([]);
  const [selectedServers, setSelectedServers] = useState(new Set());
  const [selectedJars, setSelectedJars] = useState(new Set());
  const [serverSearch, setServerSearch] = useState('');
  const [logs, setLogs] = useState([{ message: '等待部署...', level: 'info', empty: true }]);
  const [history, setHistory] = useState([]);
  const [settings, setSettings] = useState({});
  const [settingsError, setSettingsError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [serverModalOpen, setServerModalOpen] = useState(false);
  const [serverDraft, setServerDraft] = useState({ old_ip: '', ip: '', name: '', jars: [] });
  const [manualJarName, setManualJarName] = useState('');
  const [scriptOptions, setScriptOptions] = useState([]);
  const [unmatchedScripts, setUnmatchedScripts] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [uploadText, setUploadText] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [toasts, setToasts] = useState([]);
  const [scheduledTasks, setScheduledTasks] = useState([]);
  const [logHistoryOpen, setLogHistoryOpen] = useState(false);
  const [localLogFiles, setLocalLogFiles] = useState([]);
  const [logDownloadOpen, setLogDownloadOpen] = useState(false);
  const [logDownloadInfo, setLogDownloadInfo] = useState(null);
  const [logFiles, setLogFiles] = useState([]);
  const [selectedLogFiles, setSelectedLogFiles] = useState(new Set());
  const [logSaveDir, setLogSaveDir] = useState('');
  const [logLoading, setLogLoading] = useState(false);
  const [logDownloading, setLogDownloading] = useState(false);
  const [logConflicts, setLogConflicts] = useState([]);
  const [pendingLogDownload, setPendingLogDownload] = useState(null);
  const [scheduleDraft, setScheduleDraft] = useState({
    type: 'deploy',
    name: '',
    servers: [],
    jars: [],
    date: '',
    time: ''
  });
  const fileInputRef = useRef(null);
  const logRef = useRef(null);

  useEffect(() => {
    document.body.classList.toggle('dark-mode', darkMode);
    localStorage.setItem('darkMode', darkMode ? '1' : '0');
  }, [darkMode]);

  useEffect(() => {
    loadSettings();
    loadJars().then(loadServers);
    loadLogHistory();
    loadScheduledTasks();
  }, []);

  useEffect(() => {
    if (!window.io) return;
    const socket = window.io();
    socket.on('connect', () => addLog('已连接到部署服务', 'info'));
    socket.on('log', (data) => addLog(data.message, data.level));
    socket.on('download_done', (data) => {
      const message = data.fail
        ? `日志下载完成: 成功 ${data.success} 个, 失败 ${data.fail} 个`
        : `日志下载完成: ${data.success} 个文件全部成功`;
      setLogDownloading(false);
      addLog(message, data.fail ? 'warning' : 'success');
      toast(message, data.fail ? 'warning' : 'success');
    });
    socket.on('scheduled_task_created', loadScheduledTasks);
    socket.on('scheduled_task_started', loadScheduledTasks);
    socket.on('scheduled_task_done', loadScheduledTasks);
    socket.on('scheduled_task_cancelled', loadScheduledTasks);
    return () => socket.disconnect();
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  function toast(message, type = 'info') {
    const id = Date.now() + Math.random();
    setToasts((items) => [...items, { id, message, type }]);
    setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 3200);
  }

  function addLog(message, level = 'info') {
    setLogs((items) => [
      ...items.filter((item) => !item.empty),
      { message, level }
    ]);
    if (String(message).includes('部署完成') || String(message).includes('重启完成') || String(message).includes('部署已取消')) {
      setBusy(false);
      setBusyAction('');
      loadLogHistory();
    }
  }

  async function loadSettings() {
    const data = await fetch('/api/config').then((res) => res.json());
    setSettings(data);
    return data;
  }

  async function saveSettings() {
    const mode = settings.connection_mode || 'direct';
    const payload = { ...settings, connection_mode: mode, server_port: settings.server_port || '22', host_key_policy: settings.host_key_policy || 'accept-new' };
    if (!payload.server_user || (mode === 'jump' && (!payload.jump_host || !payload.jump_user))) {
      setSettingsError('请填写目标服务器账号；跳板模式还需要跳板机地址和账号');
      return;
    }
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok && data.success) {
      setSettingsOpen(false);
      toast('配置保存成功', 'success');
    } else {
      setSettingsError(data.error || '配置保存失败');
    }
  }

  async function loadServers() {
    const data = await fetch('/api/servers').then((res) => res.json());
    setServers(data);
    return data;
  }

  async function loadJars() {
    const data = await fetch('/api/jars').then((res) => res.json());
    setJars(data);
    const existingJars = new Set(data.map(getJarName));
    setSelectedJars((prev) => new Set([...prev].filter((jar) => existingJars.has(jar))));
    setScheduleDraft((draft) => {
      const jars = draft.jars.filter((jar) => existingJars.has(jar));
      const relatedServers = new Set();
      jars.forEach((jar) => findServersForJar(jar).forEach((server) => relatedServers.add(server.ip)));
      return {
        ...draft,
        jars,
        servers: draft.servers.filter((ip) => relatedServers.has(ip))
      };
    });
    return data;
  }

  async function loadLogHistory() {
    const data = await fetch('/api/deploy/history').then((res) => res.json());
    setHistory(data);
  }

  async function loadScheduledTasks() {
    const data = await fetch('/api/scheduled-tasks').then((res) => res.json());
    setScheduledTasks(data);
  }

  async function openLogHistory() {
    setLogHistoryOpen(true);
    setLocalLogFiles([]);
    const data = await fetch('/api/logs').then((res) => res.json());
    setLocalLogFiles(Array.isArray(data) ? data : []);
  }

  async function viewLocalLog(filename) {
    const data = await fetch(`/api/logs/${encodeURIComponent(filename)}`).then((res) => res.json());
    if (data.content) {
      setLogs(data.content.split('\n').map((message) => ({ message, level: 'info' })));
      setActiveTab('deploy');
      setLogHistoryOpen(false);
      toast('已加载日志文件', 'success');
    } else {
      toast(data.error || '日志读取失败', 'error');
    }
  }

  function getServerJarConfig(server, jarName) {
    return (server?.jars || []).find((jar) => getJarName(jar) === jarName);
  }

  async function openLogDownload(serverIp, jarName) {
    const server = servers.find((item) => item.ip === serverIp);
    const jarConfig = getServerJarConfig(server, jarName);
    const logDir = typeof jarConfig === 'object' ? jarConfig.log_dir : '';
    if (!logDir) {
      toast(`${jarName} 未配置日志目录，请先编辑服务器并自动获取`, 'warning');
      return;
    }

    setLogDownloadInfo({ serverIp, jarName, logDir });
    setLogSaveDir(localStorage.getItem(getLogSaveDirKey(jarName)) || '');
    setLogDownloadOpen(true);
    setLogFiles([]);
    setSelectedLogFiles(new Set());
    setLogConflicts([]);
    setPendingLogDownload(null);
    setLogLoading(true);
    const data = await fetch('/api/list-log-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server_ip: serverIp, jar_name: jarName })
    }).then((res) => res.json()).catch(() => ({ success: false, error: '请求失败，请检查网络' }));
    setLogLoading(false);
    if (data.success) {
      setLogFiles(data.files || []);
    } else {
      toast(data.error || '获取日志列表失败', 'error');
    }
  }

  function toggleLogFile(relpath) {
    setSelectedLogFiles((prev) => {
      const next = new Set(prev);
      next.has(relpath) ? next.delete(relpath) : next.add(relpath);
      return next;
    });
  }

  function selectAllLogFiles() {
    setSelectedLogFiles(new Set(logFiles.map((file) => file.relpath)));
  }

  function clearLogFileSelection() {
    setSelectedLogFiles(new Set());
  }

  async function browseLogSaveDir() {
    const data = await fetch('/api/browse-dir', { method: 'POST' }).then((res) => res.json()).catch(() => ({}));
    if (data.folder) {
      setLogSaveDir(data.folder);
      if (logDownloadInfo?.jarName) {
        localStorage.setItem(getLogSaveDirKey(logDownloadInfo.jarName), data.folder);
      }
    }
  }

  async function startLogDownload() {
    if (!logDownloadInfo) return;
    const filenames = Array.from(selectedLogFiles);
    if (!filenames.length) {
      toast('请选择要下载的日志文件', 'warning');
      return;
    }
    const saveDir = logSaveDir.trim();
    if (!saveDir) {
      toast('请选择或输入保存目录', 'warning');
      return;
    }
    localStorage.setItem(getLogSaveDirKey(logDownloadInfo.jarName), saveDir);
    const payload = { filenames, saveDir };
    setPendingLogDownload(payload);
    const data = await fetch('/api/download-logs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        server_ip: logDownloadInfo.serverIp,
        jar_name: logDownloadInfo.jarName,
        filenames,
        save_dir: saveDir,
        check_only: true
      })
    }).then((res) => res.json()).catch(() => ({ conflicts: [], error: '文件检查失败' }));
    if (data.error) {
      toast(data.error, 'error');
      return;
    }
    if ((data.conflicts || []).length) {
      setLogConflicts(data.conflicts);
      return;
    }
    doLogDownload('rename', payload);
  }

  async function doLogDownload(mode, payload = pendingLogDownload) {
    if (!logDownloadInfo || !payload) return;
    const modeLabel = mode === 'overwrite' ? '覆盖' : mode === 'rename' ? '重命名' : '跳过';
    setLogConflicts([]);
    setPendingLogDownload(null);
    setLogDownloadOpen(false);
    setLogDownloading(true);
    setLogs([]);
    addLog(`📥 开始下载 ${payload.filenames.length} 个日志文件 (${modeLabel})...`, 'info');
    const data = await fetch('/api/download-logs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        server_ip: logDownloadInfo.serverIp,
        jar_name: logDownloadInfo.jarName,
        filenames: payload.filenames,
        save_dir: payload.saveDir,
        mode
      })
    }).then((res) => res.json()).catch(() => ({ success: false, error: '下载请求失败，请检查后端服务' }));
    if (data.success) {
      toast('日志下载任务已提交，请查看日志区进度', 'info');
    } else {
      setLogDownloading(false);
      toast(data.error || '下载失败', 'error');
    }
  }

  async function stopDeploy() {
    if (!confirm('确定要停止当前部署吗？已在执行的步骤无法立即中止。')) return;
    addLog('⏹️ 正在发送停止请求...', 'warning');
    const data = await fetch('/api/deploy/cancel', { method: 'POST' }).then((res) => res.json()).catch(() => ({ success: false }));
    addLog(data.success ? '⏹️ 停止请求已发送' : '⏹️ 停止请求发送失败', data.success ? 'warning' : 'error');
  }

  function toggleSet(setter, value) {
    setter((prev) => {
      const next = new Set(prev);
      next.has(value) ? next.delete(value) : next.add(value);
      return next;
    });
  }

  function findServersForJar(jarName, serverList = servers) {
    const targetName = normalizeJarName(jarName);
    return serverList.filter((server) => (
      (server.jars || []).some((jar) => normalizeJarName(getJarName(jar)) === targetName)
    ));
  }

  function applyJarLinkage(jarNames, serverList = servers, syncDeploySelection = true) {
    const names = Array.from(new Set(jarNames.map(normalizeJarName).filter(Boolean)));
    if (!names.length) return 0;

    const relatedServers = new Set();
    names.forEach((name) => {
      findServersForJar(name, serverList).forEach((server) => relatedServers.add(server.ip));
    });

    if (syncDeploySelection) {
      setSelectedJars((prev) => new Set([...prev, ...names]));
      if (relatedServers.size) {
        setSelectedServers((prev) => new Set([...prev, ...relatedServers]));
      }
    }

    setScheduleDraft((draft) => ({
      ...draft,
      jars: names,
      servers: Array.from(relatedServers)
    }));
    return relatedServers.size;
  }

  function openScheduledTab() {
    setActiveTab('scheduled');
    loadScheduledTasks();
    if (!scheduleDraft.servers.length && !scheduleDraft.jars.length && jars.length) {
      applyJarLinkage(jars.map((jar) => jar.name), servers, false);
    }
  }

  function filteredServers() {
    const query = serverSearch.trim().toLowerCase();
    if (!query) return servers;
    return servers
      .map((server) => {
        if (server.ip.toLowerCase().includes(query) || (server.name || '').toLowerCase().includes(query)) return server;
        const matchedJars = (server.jars || []).filter((jar) => {
          const item = typeof jar === 'string' ? { name: jar, script: '' } : jar;
          return item.name.toLowerCase().includes(query) || (item.script || '').toLowerCase().includes(query);
        });
        return matchedJars.length ? { ...server, jars: matchedJars } : null;
      })
      .filter(Boolean);
  }

  async function testConnection(ip) {
    toast(`正在测试 ${ip}...`, 'info');
    const data = await fetch('/api/test-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip })
    }).then((res) => res.json());
    setServers((items) => items.map((server) => (
      server.ip === ip ? { ...server, _status: data.success ? 'ok' : 'fail' } : server
    )));
    toast(data.success ? `${ip} 连接成功` : `${ip} 连接失败`, data.success ? 'success' : 'error');
  }

  function testAllServers() {
    servers.forEach((server) => testConnection(server.ip));
  }

  async function restartServer(server) {
    const items = (server.jars || []).map((jar) => ({ server: server.ip, jar: getJarName(jar) }));
    if (!items.length) {
      toast(`${server.ip} 没有关联的JAR包`, 'warning');
      return;
    }
    if (!confirm(`确定重启 ${server.ip} 上的 ${items.length} 个服务吗？`)) return;
    await postRestart(items);
  }

  async function restartJar(ip, jarName) {
    if (!confirm(`确定重启 ${ip} 上的 ${jarName} 吗？`)) return;
    await postRestart([{ server: ip, jar: jarName }]);
  }

  async function restartJarForFile(jarName) {
    const related = findServersForJar(jarName);
    if (!related.length) {
      toast(`${jarName} 未关联任何服务器`, 'warning');
      return;
    }
    if (!confirm(`确定重启 ${related.length} 台服务器上的 ${jarName} 吗？`)) return;
    await postRestart(related.map((server) => ({ server: server.ip, jar: jarName })));
  }

  async function postRestart(items) {
    setBusy(true);
    setBusyAction('restart');
    const data = await fetch('/api/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items })
    }).then((res) => res.json());
    toast(data.success ? '重启请求已提交' : (data.error || '重启失败'), data.success ? 'success' : 'error');
    if (!data.success) {
      setBusy(false);
      setBusyAction('');
    }
  }

  async function startDeploy() {
    const selectedServerList = Array.from(selectedServers);
    const selectedJarList = Array.from(selectedJars);
    if (!selectedServerList.length || !selectedJarList.length) {
      toast('请选择服务器和JAR包', 'warning');
      return;
    }
    if (!confirm(`确定部署 ${selectedJarList.length} 个JAR到 ${selectedServerList.length} 台服务器吗？`)) return;
    setBusy(true);
    setBusyAction('deploy');
    const data = await fetch('/api/deploy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ servers: selectedServerList, jars: selectedJarList })
    }).then((res) => res.json());
    toast(data.success ? '部署请求已提交' : (data.error || '部署失败'), data.success ? 'success' : 'error');
    if (!data.success) {
      setBusy(false);
      setBusyAction('');
    }
  }

  async function startRestart() {
    const items = [];
    selectedServers.forEach((server) => {
      selectedJars.forEach((jar) => items.push({ server, jar }));
    });
    if (!items.length) {
      toast('请选择服务器和JAR包', 'warning');
      return;
    }
    if (!confirm(`确定重启 ${items.length} 个服务吗？`)) return;
    await postRestart(items);
  }

  async function uploadFiles(files) {
    const jarFiles = Array.from(files || []).filter((file) => file.name.toLowerCase().endsWith('.jar'));
    if (!jarFiles.length) {
      toast('请选择 .jar 文件', 'warning');
      return;
    }
    setUploading(true);
    let uploaded = 0;
    let failed = 0;
    const uploadedNames = [];
    let completed = 0;
    let nextIndex = 0;
    const concurrency = Math.min(3, jarFiles.length);

    async function uploadOne(file) {
      setUploadText(`上传中 ${completed}/${jarFiles.length}：${file.name}`);
      const form = new FormData();
      form.append('file', file);
      try {
        const res = await fetch('/api/jars', { method: 'POST', body: form });
        const data = await readActionResult(res);
        uploaded++;
        uploadedNames.push(normalizeJarName(data.name || file.name));
      } catch (error) {
        failed++;
        addLog(`JAR上传失败：${file.name} - ${error.message || '上传失败'}`, 'warning');
      } finally {
        completed++;
        setUploadText(`上传中 ${completed}/${jarFiles.length}`);
      }
    }

    async function uploadWorker() {
      while (nextIndex < jarFiles.length) {
        const file = jarFiles[nextIndex++];
        await uploadOne(file);
      }
    }

    await Promise.all(Array.from({ length: concurrency }, uploadWorker));
    setUploading(false);
    setUploadText('');
    await loadJars();
    const nextServers = await loadServers();
    const linkedServers = applyJarLinkage(uploadedNames, nextServers);
    const suffix = linkedServers ? `，已自动关联 ${linkedServers} 台服务器` : '，暂无匹配服务器配置';
    toast(failed ? `上传成功 ${uploaded} 个，失败 ${failed} 个${suffix}` : `上传了 ${uploaded} 个JAR包${suffix}`, failed ? 'warning' : 'success');
  }

  async function deleteJars(names, confirmMessage) {
    const uniqueNames = Array.from(new Set(names)).filter((name) => jars.some((jar) => jar.name === name));
    if (!uniqueNames.length) {
      toast('请选择要删除的JAR包', 'warning');
      return;
    }
    if (confirmMessage && !confirm(confirmMessage)) return;

    const deletedNames = [];
    const failedNames = [];
    for (const name of uniqueNames) {
      try {
        const res = await fetch(`/api/jars/${encodeURIComponent(name)}`, { method: 'DELETE' });
        await readActionResult(res);
        deletedNames.push(name);
      } catch (error) {
        failedNames.push(`${name}: ${error.message || '删除失败'}`);
      }
    }

    await loadJars();
    if (deletedNames.length) {
      const deletedSet = new Set(deletedNames);
      setSelectedJars((prev) => new Set([...prev].filter((jar) => !deletedSet.has(jar))));
      setScheduleDraft((draft) => {
        const remainingJars = draft.jars.filter((jar) => !deletedSet.has(jar));
        const relatedServers = new Set();
        remainingJars.forEach((jar) => findServersForJar(jar).forEach((server) => relatedServers.add(server.ip)));
        return {
          ...draft,
          jars: remainingJars,
          servers: draft.servers.filter((ip) => relatedServers.has(ip))
        };
      });
    }

    if (failedNames.length) {
      toast(`删除成功 ${deletedNames.length} 个，失败 ${failedNames.length} 个`, deletedNames.length ? 'warning' : 'error');
      addLog(`JAR批量删除失败：${failedNames.join('；')}`, 'warning');
      return;
    }
    toast(deletedNames.length === 1 ? `删除成功：${deletedNames[0]}` : `已删除 ${deletedNames.length} 个JAR包`, 'success');
  }

  async function deleteJar(name) {
    await deleteJars([name], `确定删除 ${name} 吗？`);
  }

  async function deleteSelectedJars() {
    await deleteJars(selectedJarNames, `确定删除选中的 ${selectedJarNames.length} 个JAR包吗？`);
  }

  function toggleAllJars() {
    if (!jars.length) return;
    if (allJarsSelected) {
      setSelectedJars(new Set());
      return;
    }
    setSelectedJars(new Set(jars.map((jar) => jar.name)));
  }

  function openServerModal(server = null) {
    setScriptOptions([]);
    setUnmatchedScripts([]);
    setServerDraft(server
      ? { old_ip: server.ip, ip: server.ip, name: server.name || '', jars: (server.jars || []).map(normalizeServerJar) }
      : { old_ip: '', ip: '', name: '', jars: [] });
    setManualJarName('');
    setServerModalOpen(true);
  }

  function normalizeServerJar(jar) {
    return typeof jar === 'string'
      ? { name: jar, script: '', log_dir: '' }
      : { name: jar.name || '', script: jar.script || '', log_dir: jar.log_dir || '' };
  }

  function updateServerJar(index, patch) {
    setServerDraft((draft) => ({
      ...draft,
      jars: draft.jars.map((jar, i) => (i === index ? { ...jar, ...patch } : jar))
    }));
  }

  function removeServerJar(index) {
    setServerDraft((draft) => ({
      ...draft,
      jars: draft.jars.filter((_, i) => i !== index)
    }));
  }

  function addServerJar(name) {
    const trimmed = name.trim();
    if (!trimmed) return;
    setServerDraft((draft) => (
      draft.jars.some((jar) => jar.name === trimmed)
        ? draft
        : { ...draft, jars: [...draft.jars, { name: trimmed, script: '', log_dir: '' }] }
    ));
    setManualJarName('');
  }

  async function discoverServerJars() {
    if (!serverDraft.ip.trim()) {
      toast('请先输入服务器IP', 'warning');
      return;
    }
    toast('正在从服务器获取JAR包、脚本和日志目录...', 'info');
    const data = await fetch('/api/discover-jars', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip: serverDraft.ip.trim() })
    }).then((res) => res.json());
    if (!data.success) {
      toast(data.error || '获取失败', 'error');
      return;
    }

    setScriptOptions((data.jars || []).map((jar) => jar.script).filter(Boolean));
    setUnmatchedScripts(data.unmatched_scripts || []);
    setServerDraft((draft) => {
      const byName = new Map(draft.jars.map((jar) => [jar.name, normalizeServerJar(jar)]));
      (data.jars || []).forEach((jar) => {
        const current = byName.get(jar.name) || { name: jar.name, script: '', log_dir: '' };
        byName.set(jar.name, {
          ...current,
          script: jar.script || current.script || '',
          log_dir: jar.log_dir || current.log_dir || ''
        });
      });
      return { ...draft, jars: Array.from(byName.values()) };
    });

    const needManual = (data.jars || []).filter((jar) => !jar.script).length;
    const unmatched = (data.unmatched_scripts || []).length;
    toast(`获取到 ${(data.jars || []).length} 个JAR，${needManual} 个需手动选择脚本，${unmatched} 个脚本未匹配`, needManual ? 'warning' : 'success');
  }

  async function saveServer() {
    const payload = {
      ...serverDraft,
      ip: serverDraft.ip.trim(),
      name: serverDraft.name.trim()
    };
    const method = serverDraft.old_ip ? 'PUT' : 'POST';
    const data = await fetch('/api/servers', {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then((res) => res.json());
    if (data.success) {
      setServerModalOpen(false);
      await loadServers();
      toast('保存成功', 'success');
    }
  }

  async function deleteServerByIp(ip) {
    if (!ip || !confirm(`确定删除服务器 ${ip} 吗？`)) return;
    try {
      const res = await fetch('/api/servers', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip })
      });
      await readActionResult(res);
    } catch (error) {
      await loadServers();
      toast(error.message || '删除失败', 'error');
      return;
    }
    setSelectedServers((prev) => {
      const next = new Set(prev);
      next.delete(ip);
      return next;
    });
    setServerModalOpen(false);
    await loadServers();
    toast('删除成功', 'success');
  }

  function setScheduleServers(values) {
    setScheduleDraft((draft) => ({ ...draft, servers: values }));
  }

  async function createScheduledTask() {
    const taskServers = Array.from(new Set(scheduleDraft.servers));
    const taskJars = Array.from(new Set(scheduleDraft.jars));
    if (!taskServers.length) {
      toast('请选择目标服务器', 'warning');
      return;
    }
    if (!taskJars.length) {
      toast('请先上传JAR包以自动关联定时任务', 'warning');
      return;
    }
    if (!scheduleDraft.date || !/^\d{1,2}:\d{2}$/.test(scheduleDraft.time)) {
      toast('请选择日期并输入 HH:MM 时间', 'warning');
      return;
    }
    const data = await fetch('/api/scheduled-tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: scheduleDraft.type,
        name: scheduleDraft.name,
        servers: taskServers,
        jars: taskJars,
        scheduled_at: `${scheduleDraft.date}T${scheduleDraft.time}:00`
      })
    }).then((res) => res.json());
    if (data.success) {
      setScheduleDraft((draft) => ({ ...draft, name: '', date: '', time: '' }));
      await loadScheduledTasks();
      toast('定时任务创建成功', 'success');
    } else {
      toast(data.error || '创建失败', 'error');
    }
  }

  async function deleteScheduledTask(taskId) {
    if (!confirm('确定删除或取消此定时任务？')) return;
    const data = await fetch(`/api/scheduled-tasks/${taskId}`, { method: 'DELETE' }).then((res) => res.json());
    toast(data.success ? '任务已更新' : (data.error || '操作失败'), data.success ? 'success' : 'error');
    await loadScheduledTasks();
  }

  const visibleServers = filteredServers();
  const currentJarNames = jars.map((jar) => jar.name);
  const selectedJarNames = currentJarNames.filter((name) => selectedJars.has(name));
  const allJarsSelected = currentJarNames.length > 0 && selectedJarNames.length === currentJarNames.length;
  const selectedInfo = `已选: ${selectedServers.size} 服务器, ${selectedJarNames.length} JAR包`;
  const jarOptions = Array.from(new Set([
    ...jars.map((jar) => jar.name),
    ...servers.flatMap((server) => (server.jars || []).map(getJarName))
  ])).sort();
  const scheduleServerOptions = scheduleDraft.jars.length
    ? servers.filter((server) => (server.jars || []).some((jar) => scheduleDraft.jars.includes(getJarName(jar))))
    : servers;
  const availableScriptOptions = Array.from(new Set([...scriptOptions, ...unmatchedScripts])).sort();
  const pendingScheduled = scheduledTasks.filter((task) => task.status === 'pending').length;

  return (
    <>
      <nav className="navbar navbar-expand-lg navbar-dark mb-4">
        <div className="container">
          <div className="d-flex align-items-center">
            <i className="bi bi-rocket-takeoff me-2" />
            <span className="navbar-brand mb-0 h1">JAR包部署管理系统</span>
          </div>
          <div className="d-flex align-items-center gap-2">
            <button id="darkModeToggle" onClick={() => setDarkMode((value) => !value)} title="切换深色模式">
              <i className={`bi ${darkMode ? 'bi-sun' : 'bi-moon-stars'}`} />
            </button>
            <button className="btn btn-sm nav-ghost-btn" onClick={() => setSettingsOpen(true)}>
              <i className="bi bi-gear" /> 设置
            </button>
          </div>
        </div>
      </nav>

      <main className="container app-main">
        <div className="main-layout">
          <aside className="left-col">
            <section className="card mb-4">
              <div className="card-header d-flex justify-content-between align-items-center">
                <span><span className="section-icon server"><i className="bi bi-hdd-network" /></span>服务器列表 <span className="section-count">{visibleServers.length}台</span></span>
                <div>
                  <button className="btn btn-sm btn-outline-success me-1" onClick={testAllServers}><i className="bi bi-wifi" /> 全测</button>
                  <button className="btn btn-sm btn-outline-primary" onClick={() => openServerModal()}><i className="bi bi-plus-lg" /> 添加</button>
                </div>
              </div>
              <div className="card-body">
                <div className="server-search-wrap">
                  <i className="bi bi-search server-search-icon" />
                  <input className="server-search-input" value={serverSearch} onChange={(e) => setServerSearch(e.target.value)} placeholder="搜索 名称 / IP / JAR / 脚本..." />
                  {serverSearch && <i className="bi bi-x-lg server-search-clear" onClick={() => setServerSearch('')} />}
                </div>
                <div className="scroll-pane server-pane">
                  {visibleServers.length === 0 ? <Empty icon="bi-hdd-network" text="暂无服务器，请点击添加" /> : visibleServers.map((server) => (
                    <div key={server.ip} className={`server-item ${selectedServers.has(server.ip) ? 'selected' : ''}`} data-status={server._status || ''} onClick={() => toggleSet(setSelectedServers, server.ip)}>
                      <div className="d-flex justify-content-between align-items-center">
                        <div className="d-flex align-items-center gap-2">
                          <input type="checkbox" className="form-check-input" checked={selectedServers.has(server.ip)} onChange={() => toggleSet(setSelectedServers, server.ip)} onClick={(e) => e.stopPropagation()} />
                          <span className="server-identity">
                            <span className="server-name-label">{server.name || '未命名服务器'}</span>
                            <span className="ip-label">{server.ip}</span>
                          </span>
                          {server._status && <span className={`badge ${server._status === 'ok' ? 'bg-success' : 'bg-danger'}`}>{server._status === 'ok' ? '在线' : '离线'}</span>}
                        </div>
                        <div className="d-flex gap-1">
                          <ActionIcon type="restart" icon="bi-arrow-repeat" title="重启该服务器所有服务" onClick={(e) => { e.stopPropagation(); restartServer(server); }} />
                          <ActionIcon type="wifi" icon="bi-wifi" title="测试连接" onClick={(e) => { e.stopPropagation(); testConnection(server.ip); }} />
                          <ActionIcon type="edit" icon="bi-pencil" title="编辑" onClick={(e) => { e.stopPropagation(); openServerModal(server); }} />
                          <ActionIcon type="delete" icon="bi-trash3" title="删除" onClick={(e) => { e.stopPropagation(); deleteServerByIp(server.ip); }} />
                        </div>
                      </div>
                      {(server.jars || []).length > 0 && (
                        <div className="server-jars">
                          {(server.jars || []).map((jar) => {
                            const name = getJarName(jar);
                            const hasFile = jars.some((file) => file.name === name);
                            return (
                              <div key={name} className="server-jar-row" role="button" tabIndex={0} onClick={(e) => { e.stopPropagation(); restartJar(server.ip, name); }} onKeyDown={(e) => { if (e.key === 'Enter') restartJar(server.ip, name); }}>
                                <i className="bi bi-arrow-repeat jar-restart-icon" />
                                <span className="jar-name-text">{name}</span>
                                <span className={`jar-local-tag ${hasFile ? 'has' : 'missing'}`}>{hasFile ? '本地有' : '本地无'}</span>
                                <button type="button" className="jar-row-action" title="下载日志" onClick={(e) => { e.stopPropagation(); openLogDownload(server.ip, name); }}>
                                  <i className="bi bi-file-earmark-arrow-down" />
                                </button>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </section>

            <section className="card mb-4">
              <div className="card-header d-flex justify-content-between align-items-center">
                <span><span className="section-icon jar"><i className="bi bi-box-seam" /></span>JAR包列表 <span className="section-count">{jars.length}个</span></span>
                <div className="d-flex align-items-center gap-2">
                  {uploading && <span className="upload-progress"><span className="spinner-border text-primary" />{uploadText || '上传中...'}</span>}
                  <button type="button" className="btn btn-sm btn-outline-secondary" disabled={!jars.length} onClick={toggleAllJars}>{allJarsSelected ? '取消全选' : '全选'}</button>
                  <button type="button" className="btn btn-sm btn-outline-danger" disabled={!selectedJarNames.length} onClick={deleteSelectedJars}><i className="bi bi-trash3" /> 删除选中{selectedJarNames.length ? `(${selectedJarNames.length})` : ''}</button>
                  <button className="btn btn-sm btn-outline-primary" onClick={() => fileInputRef.current?.click()}><i className="bi bi-upload" /> 上传</button>
                </div>
                <input ref={fileInputRef} type="file" hidden multiple accept=".jar" onChange={async (e) => { await uploadFiles(e.target.files); e.target.value = ''; }} />
              </div>
              <div className="card-body scroll-pane jar-pane" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); uploadFiles(e.dataTransfer.files); }}>
                <button className="drop-zone" onClick={() => fileInputRef.current?.click()}>
                  <i className="bi bi-cloud-upload" />
                  <span>拖拽 JAR 包到此处，或点击上传</span>
                </button>
                {jars.map((jar) => {
                  const related = servers.filter((server) => (server.jars || []).some((item) => getJarName(item) === jar.name));
                  return (
                    <div key={jar.name} className={`jar-item ${selectedJars.has(jar.name) ? 'selected' : ''}`} onClick={() => toggleSet(setSelectedJars, jar.name)}>
                      <input type="checkbox" className="form-check-input" checked={selectedJars.has(jar.name)} onChange={() => toggleSet(setSelectedJars, jar.name)} onClick={(e) => e.stopPropagation()} />
                      <div className="flex-grow-1 overflow-hidden">
                        <div className="jar-name text-truncate">{jar.name}</div>
                        <div className="jar-meta">{formatSize(jar.size)} · {related.length ? related.map((server) => server.ip).join(', ') : '未关联服务器'}</div>
                      </div>
                      <ActionIcon type="restart" icon="bi-arrow-repeat" title="重启关联服务" onClick={(e) => { e.stopPropagation(); restartJarForFile(jar.name); }} />
                      <ActionIcon type="delete" icon="bi-trash3" title="删除JAR" onClick={(e) => { e.stopPropagation(); deleteJar(jar.name); }} />
                    </div>
                  );
                })}
              </div>
            </section>
          </aside>

          <section className="right-col">
            <div className="card mb-4">
              <div className="card-header d-flex justify-content-between align-items-center p-0">
                <ul className="nav nav-tabs card-header-tabs px-3" role="tablist">
                  <TabButton id="deploy" activeTab={activeTab} setActiveTab={setActiveTab} icon="bi-play-circle" label="部署控制" />
                  <TabButton id="history" activeTab={activeTab} setActiveTab={setActiveTab} icon="bi-clock-history" label="历史记录" />
                  <TabButton id="scheduled" activeTab={activeTab} setActiveTab={setActiveTab} icon="bi-calendar-check" label="定时任务" badge={pendingScheduled} onClick={openScheduledTab} />
                </ul>
                {busy && <div className="progress active-progress me-3"><div className="progress-bar active" /></div>}
              </div>

              {activeTab === 'deploy' && (
                <div>
                  <div className="log-header">
                    <span><i className="bi bi-terminal me-1" />部署日志</span>
                    <div className="log-header-actions">
                      <button className="btn btn-sm py-0 px-2 log-clear" onClick={openLogHistory} title="查看本地日志"><i className="bi bi-clock-history me-1" />历史</button>
                      <button className="btn btn-sm py-0 px-2 log-clear" onClick={() => setLogs([])} title="清空日志"><i className="bi bi-trash3" /></button>
                    </div>
                  </div>
                  <div className="log-container" ref={logRef}>
                    {logs.length === 0 ? <Empty icon="bi-terminal" text="等待部署..." /> : logs.map((log, index) => <div key={index} className={`log-entry log-${log.level}`}>{log.message}</div>)}
                  </div>
                  <div className="card-body deploy-actions">
                    <span className="selected-info">{selectedInfo}</span>
                    <div className="d-flex gap-2">
                      {busyAction === 'deploy' && <button className="btn btn-outline-danger" onClick={stopDeploy}><i className="bi bi-stop-circle me-1" />停止部署</button>}
                      <button className="btn btn-warning" disabled={busy || !selectedServers.size || !selectedJars.size} onClick={startRestart}><i className="bi bi-arrow-repeat me-1" />重启服务</button>
                      <button className="btn btn-primary" disabled={busy || !selectedServers.size || !selectedJars.size} onClick={startDeploy}><i className="bi bi-rocket-takeoff me-1" />开始部署</button>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'history' && (
                <div className="card-body scroll-pane history-pane">
                  {!history.length ? <Empty icon="bi-clock-history" text="暂无历史记录" /> : history.slice(0, 30).map((row) => (
                    <div className="list-group-item" key={row.id || row.created_at}>
                      <div className="d-flex justify-content-between align-items-center">
                        <strong className={row.status === 'done' || row.status === 'restart' ? 'text-success' : 'text-danger'}>{row.status}</strong>
                        <small className="text-muted">{row.created_at || ''}</small>
                      </div>
                      <div className="small text-muted">服务器: {(row.servers || []).join(', ') || '-'}</div>
                      <div className="small text-muted">JAR: {(row.jars || []).join(', ') || '-'}</div>
                      <div className="small">成功 {row.success_count}，失败 {row.fail_count}，耗时 {row.duration_seconds || 0}s</div>
                    </div>
                  ))}
                </div>
              )}

              {activeTab === 'scheduled' && (
                <ScheduledPanel
                  draft={scheduleDraft}
                  setDraft={setScheduleDraft}
                  serverOptions={scheduleServerOptions}
                  setScheduleServers={setScheduleServers}
                  createScheduledTask={createScheduledTask}
                  tasks={scheduledTasks}
                  deleteScheduledTask={deleteScheduledTask}
                />
              )}
            </div>
          </section>
        </div>
      </main>

      {settingsOpen && (
        <Modal
          title="系统设置"
          icon="bi-gear"
          onClose={() => setSettingsOpen(false)}
          footer={<><button className="btn btn-outline-secondary" onClick={() => setSettingsOpen(false)}>取消</button><button className="btn btn-primary" onClick={saveSettings}>保存配置</button></>}
        >
          {settingsError && <div className="alert alert-danger py-2">{settingsError}</div>}
          <div className="settings-grid">
            <label className="form-field"><span>连接方式</span><select className="form-select" value={settings.connection_mode || 'direct'} onChange={(e) => setSettings((data) => ({ ...data, connection_mode: e.target.value }))}><option value="direct">直连</option><option value="jump">单级跳板</option></select></label>
            <label className="form-field"><span>目标服务器端口</span><input className="form-control" type="number" value={settings.server_port || '22'} onChange={(e) => setSettings((data) => ({ ...data, server_port: e.target.value }))} /></label>
            <label className="form-field"><span>主机密钥策略</span><select className="form-select" value={settings.host_key_policy || 'accept-new'} onChange={(e) => setSettings((data) => ({ ...data, host_key_policy: e.target.value }))}><option value="accept-new">首次接受并记录</option><option value="strict">仅已知主机</option></select></label>
          </div>
          {settings.connection_mode === 'jump' && <div className="settings-grid"><label className="form-field"><span>跳板机地址</span><input className="form-control" value={settings.jump_host || ''} onChange={(e) => setSettings((data) => ({ ...data, jump_host: e.target.value }))} /></label><label className="form-field"><span>跳板机端口</span><input className="form-control" type="number" value={settings.jump_port || '22'} onChange={(e) => setSettings((data) => ({ ...data, jump_port: e.target.value }))} /></label></div>}
          <AuthFields prefix="server" title="目标服务器认证" settings={settings} setSettings={setSettings} />
          {settings.connection_mode === 'jump' && <AuthFields prefix="jump" title="跳板机认证" settings={settings} setSettings={setSettings} />}
          <div className="settings-grid">
            {settingsFields.map(([key, label, type, placeholder]) => (
              <label className="form-field" key={key}>
                <span>{label}</span>
                <input className="form-control" type={type} placeholder={placeholder} value={settings[key] || ''} onChange={(e) => setSettings((data) => ({ ...data, [key]: e.target.value }))} />
              </label>
            ))}
          </div>
        </Modal>
      )}

      {serverModalOpen && (
        <Modal
          title={serverDraft.old_ip ? '编辑服务器' : '添加服务器'}
          icon="bi-server"
          onClose={() => setServerModalOpen(false)}
          footer={<><button className="btn btn-danger me-auto" style={{ display: serverDraft.old_ip ? '' : 'none' }} onClick={() => deleteServerByIp(serverDraft.old_ip)}>删除</button><button className="btn btn-outline-secondary" onClick={() => setServerModalOpen(false)}>取消</button><button className="btn btn-primary" onClick={saveServer}>保存</button></>}
        >
          <label className="form-field mb-3">
            <span>服务器名称/用途</span>
            <input className="form-control" value={serverDraft.name} onChange={(e) => setServerDraft((draft) => ({ ...draft, name: e.target.value }))} placeholder="例如：GIS服务 / 查询服务 / 批处理服务" />
          </label>
          <label className="form-field mb-3">
            <span>服务器IP</span>
            <input className="form-control" value={serverDraft.ip} onChange={(e) => setServerDraft((draft) => ({ ...draft, ip: e.target.value }))} placeholder="10.0.0.2" />
          </label>
          <div className="jar-config-toolbar">
            <div className="uploaded-jar-strip">
              {jars.length ? jars.map((jar) => (
                <button type="button" className="jar-uploaded-badge" key={jar.name} onClick={() => addServerJar(jar.name)}>+ {jar.name}</button>
              )) : <span className="text-muted small">暂无已上传JAR包</span>}
            </div>
            <div className="jar-add-row">
              <input
                className="form-control form-control-sm"
                value={manualJarName}
                onChange={(e) => setManualJarName(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addServerJar(manualJarName); } }}
                placeholder="输入JAR包名后回车添加"
              />
              <button type="button" className="btn btn-sm btn-outline-primary" onClick={() => addServerJar(manualJarName)}>添加</button>
              <button type="button" className="btn btn-sm btn-outline-success" onClick={discoverServerJars}><i className="bi bi-search me-1" />自动获取</button>
            </div>
          </div>
          <div className="jar-config-list">
            {serverDraft.jars.length ? serverDraft.jars.map((jar, index) => (
              <div className="jar-config-card" key={`${jar.name}-${index}`}>
                <div className="jar-config-header">
                  <i className="bi bi-box-seam" />
                  <strong title={jar.name}>{jar.name}</strong>
                  <button type="button" className="action-icon delete" title="删除" onClick={() => removeServerJar(index)}><i className="bi bi-x-lg" /></button>
                </div>
                <label className="jar-config-field">
                  <span>脚本</span>
                  {availableScriptOptions.length && !jar.script ? (
                    <select className="form-select form-select-sm" value={jar.script} onChange={(e) => updateServerJar(index, { script: e.target.value })}>
                      <option value="">-- 选择脚本 --</option>
                      {availableScriptOptions.map((script) => <option key={script} value={script}>{script}</option>)}
                    </select>
                  ) : (
                    <input className="form-control form-control-sm" value={jar.script || ''} onChange={(e) => updateServerJar(index, { script: e.target.value })} placeholder="脚本文件名" />
                  )}
                </label>
                <label className="jar-config-field">
                  <span>日志</span>
                  <input className="form-control form-control-sm" value={jar.log_dir || ''} onChange={(e) => updateServerJar(index, { log_dir: e.target.value })} placeholder="日志子目录名" />
                </label>
              </div>
            )) : <Empty icon="bi-box-seam" text="暂无关联JAR，可手动添加或自动获取" />}
          </div>
        </Modal>
      )}

      {logHistoryOpen && (
        <LocalLogHistoryModal
          files={localLogFiles}
          viewLocalLog={viewLocalLog}
          onClose={() => setLogHistoryOpen(false)}
        />
      )}

      {logDownloadOpen && (
        <LogDownloadModal
          info={logDownloadInfo}
          files={logFiles}
          selected={selectedLogFiles}
          saveDir={logSaveDir}
          loading={logLoading}
          downloading={logDownloading}
          conflicts={logConflicts}
          setSaveDir={setLogSaveDir}
          toggleFile={toggleLogFile}
          selectAll={selectAllLogFiles}
          clearSelection={clearLogFileSelection}
          browseDir={browseLogSaveDir}
          startDownload={startLogDownload}
          confirmDownload={doLogDownload}
          onClose={() => setLogDownloadOpen(false)}
        />
      )}

      <div className="toast-stack">
        {toasts.map((item) => <div className={`react-toast toast-${item.type}`} key={item.id}>{item.message}</div>)}
      </div>
    </>
  );
}

function ActionIcon({ type, icon, title, onClick }) {
  return <button className={`action-icon ${type}`} title={title} onClick={onClick}><i className={`bi ${icon}`} /></button>;
}

function Empty({ icon, text }) {
  return <div className="empty-state"><i className={`bi ${icon}`} /><p>{text}</p></div>;
}

function TabButton({ id, activeTab, setActiveTab, icon, label, badge, onClick }) {
  return (
    <li className="nav-item">
      <button className={`nav-link ${activeTab === id ? 'active' : ''}`} onClick={() => { setActiveTab(id); onClick?.(); }}>
        <i className={`bi ${icon} me-1`} />{label}
        {!!badge && <span className="badge bg-warning text-dark ms-1">{badge}</span>}
      </button>
    </li>
  );
}

function LocalLogHistoryModal({ files, viewLocalLog, onClose }) {
  return (
    <Modal
      title="本地日志"
      icon="bi-clock-history"
      onClose={onClose}
      footer={<button className="btn btn-outline-secondary" onClick={onClose}>关闭</button>}
    >
      <div className="log-history-list">
        {!files.length ? <Empty icon="bi-file-earmark-text" text="暂无本地日志文件" /> : files.slice(0, 40).map((file) => (
          <button key={file} type="button" className="log-history-row" onClick={() => viewLocalLog(file)}>
            <i className="bi bi-file-earmark-text" />
            <span>{file}</span>
          </button>
        ))}
      </div>
    </Modal>
  );
}

function LogDownloadModal({ info, files, selected, saveDir, loading, downloading, conflicts, setSaveDir, toggleFile, selectAll, clearSelection, browseDir, startDownload, confirmDownload, onClose }) {
  const totalSize = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  return (
    <Modal
      title="下载日志"
      icon="bi-file-earmark-arrow-down"
      onClose={onClose}
      footer={<>
        <button className="btn btn-outline-secondary" onClick={onClose}>关闭</button>
        {conflicts.length ? (
          <>
            <button className="btn btn-danger" onClick={() => confirmDownload('overwrite')}>覆盖同名文件</button>
            <button className="btn btn-warning" onClick={() => confirmDownload('rename')}>重命名保留</button>
          </>
        ) : (
          <button className="btn btn-primary" disabled={loading || downloading || !selected.size} onClick={startDownload}>下载选中文件</button>
        )}
      </>}
    >
      <div className="log-modal-title">{info?.jarName || '-'} <span>{info?.serverIp || ''}</span></div>
      <div className="log-modal-meta">日志目录: {info?.logDir || '-'}</div>
      <div className="log-file-toolbar">
        <span>{files.length ? `共 ${files.length} 个文件 (${formatSize(totalSize)})` : '今天没有日志文件'}</span>
        <div>
          <button type="button" onClick={selectAll}>全选</button>
          <button type="button" onClick={clearSelection}>取消</button>
        </div>
      </div>
      <div className="log-file-list">
        {loading ? <Empty icon="bi-hourglass-split" text="正在加载日志文件..." /> : files.map((file) => (
          <label className="log-file-row" key={file.relpath}>
            <input type="checkbox" checked={selected.has(file.relpath)} onChange={() => toggleFile(file.relpath)} />
            <span className="log-file-name">{file.relpath}</span>
            <span>{formatSize(Number(file.size || 0))}</span>
            <span>{file.mtime}</span>
          </label>
        ))}
      </div>
      <div className="input-group mt-3">
        <span className="input-group-text">保存到</span>
        <input className="form-control" value={saveDir} onChange={(e) => setSaveDir(e.target.value)} placeholder="输入或选择保存目录路径" />
        <button className="btn btn-outline-secondary" type="button" onClick={browseDir}><i className="bi bi-folder2-open" /></button>
      </div>
      {!!conflicts.length && (
        <div className="log-conflict-panel mt-3">
          <strong>文件冲突</strong>
          <span>以下 {conflicts.length} 个文件已存在: {conflicts.join(', ')}</span>
        </div>
      )}
    </Modal>
  );
}

function ScheduledPanel({ draft, setDraft, serverOptions, setScheduleServers, createScheduledTask, tasks, deleteScheduledTask }) {
  const statusMap = {
    pending: ['bg-warning text-dark', '等待中'],
    running: ['bg-primary', '执行中'],
    completed: ['bg-success', '已完成'],
    failed: ['bg-danger', '失败'],
    cancelled: ['bg-secondary', '已取消'],
    expired: ['bg-dark', '已过期']
  };
  const typeMap = { deploy: '部署', restart: '重启' };

  return (
    <div>
      <div className="card-body scheduled-form">
        <div className="schedule-toolbar">
          <div className="schedule-type">
            <label><input type="radio" name="scheduleType" checked={draft.type === 'deploy'} onChange={() => setDraft((data) => ({ ...data, type: 'deploy' }))} />部署更新</label>
            <label><input type="radio" name="scheduleType" checked={draft.type === 'restart'} onChange={() => setDraft((data) => ({ ...data, type: 'restart' }))} />仅重启</label>
          </div>
          <input className="form-control form-control-sm" value={draft.name} onChange={(e) => setDraft((data) => ({ ...data, name: e.target.value }))} placeholder="任务名称（可选）" />
          <input type="date" className="form-control form-control-sm date-input" value={draft.date} onChange={(e) => setDraft((data) => ({ ...data, date: e.target.value }))} />
          <input className="form-control form-control-sm time-input" value={draft.time} onChange={(e) => setDraft((data) => ({ ...data, time: e.target.value }))} placeholder="02:00" maxLength={5} />
          <button className="btn btn-sm btn-primary" onClick={createScheduledTask}><i className="bi bi-plus-lg" /> 创建任务</button>
        </div>
        <div className="row g-2">
          <div className="col-md-6">
            <label className="form-label small mb-1">目标服务器</label>
            <select className="form-select form-select-sm schedule-multi-select" multiple value={draft.servers} onChange={(e) => setScheduleServers(Array.from(e.target.selectedOptions).map((opt) => opt.value))}>
              {serverOptions.map((server) => <option key={server.ip} value={server.ip}>{server.ip} ({(server.jars || []).length}个JAR)</option>)}
            </select>
          </div>
          <div className="col-md-6">
            <label className="form-label small mb-1">JAR包</label>
            <select className="form-select form-select-sm schedule-multi-select schedule-jar-select" multiple size={10} value={draft.jars} disabled title="JAR包由上传自动关联，不能手动选择">
              {draft.jars.length
                ? draft.jars.map((jar) => <option key={jar} value={jar}>{jar}</option>)
                : <option value="">上传JAR后自动关联</option>}
            </select>
            <div className="schedule-readonly-note">JAR包由上传自动关联，删除JAR会同步清理。</div>
          </div>
        </div>
      </div>
      <div className="card-body scroll-pane scheduled-pane">
        {!tasks.length ? <Empty icon="bi-calendar-check" text="暂无定时任务" /> : tasks.slice().sort((a, b) => b.id.localeCompare(a.id)).map((task) => {
          const [cls, label] = statusMap[task.status] || ['bg-secondary', task.status];
          const time = task.scheduled_at ? task.scheduled_at.replace('T', ' ').slice(0, 16) : '-';
          return (
            <div className="scheduled-row" key={task.id}>
              <strong>{task.name}</strong>
              <span className="badge bg-light text-dark border">{typeMap[task.type] || task.type}</span>
              <span className="small text-muted">{(task.servers || []).join(', ')}</span>
              <span className="small text-muted">{(task.jars || []).length}个JAR</span>
              <span className="small">{time}</span>
              <span className={`badge ${cls}`}>{label}</span>
              {task.error && <span className="small text-danger" title={task.error}>{task.error.slice(0, 20)}</span>}
              {task.status !== 'running' && <ActionIcon type="delete" icon="bi-x-lg" title={task.status === 'pending' ? '取消任务' : '删除记录'} onClick={() => deleteScheduledTask(task.id)} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
