/*
 * 在 bigmodel.cn 已登录页面 → F12 → Console → 粘贴本脚本 → 回车
 * 自动把 document.cookie + localStorage 拼成 JSON 拷到剪贴板
 * 然后在 GUI 里点"从剪贴板导入"即可
 */
(function () {
  // 解析 document.cookie
  const cookies = document.cookie.split('; ').filter(Boolean).map(c => {
    const i = c.indexOf('=');
    return {
      name: c.slice(0, i),
      value: decodeURIComponent(c.slice(i + 1)),
      path: '/',
    };
  });

  // localStorage 全拷
  const localStorage = {};
  for (let i = 0; i < window.localStorage.length; i++) {
    const k = window.localStorage.key(i);
    localStorage[k] = window.localStorage.getItem(k);
  }

  const payload = {
    domain: location.hostname,
    cookies,
    localStorage,
  };

  const json = JSON.stringify(payload, null, 2);
  console.log('=== 导出的 JSON (前 500 字符) ===');
  console.log(json.slice(0, 500) + (json.length > 500 ? '\n...(已截断)' : ''));
  console.log(`\n=== 统计: ${cookies.length} cookies, ${Object.keys(localStorage).length} localStorage 项 ===`);

  // 尝试复制到剪贴板
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(json).then(
      () => alert('✓ JSON 已复制到剪贴板,直接到 GUI 点"从剪贴板导入"'),
      () => fallback()
    );
  } else {
    fallback();
  }

  function fallback() {
    // 老浏览器:用 textarea + execCommand
    const ta = document.createElement('textarea');
    ta.value = json;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      alert('✓ JSON 已复制到剪贴板 (fallback)');
    } catch (e) {
      alert('❌ 自动复制失败,请手动从 console 复制:\n\n' + json.slice(0, 200) + '...');
    }
    document.body.removeChild(ta);
  }
})();