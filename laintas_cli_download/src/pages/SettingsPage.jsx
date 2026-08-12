import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { useAuth } from '../contexts/AuthContext';
import { useLanguage } from '../contexts/LanguageContext';
import { useTheme } from '../contexts/ThemeContext';

export default function SettingsPage() {
  const { t, lang, toggleLang } = useLanguage();
  const { theme, toggleTheme } = useTheme();
  const auth = useAuth();
  const { data: session, isPending } = auth.useSession();
  const [signingOut, setSigningOut] = useState(false);
  const [balance, setBalance] = useState(null);
  // The CLI's own monthly allowance. Sold here rather than on a central price
  // list, because this is the page somebody lands on when it runs out.
  const [calls, setCalls] = useState(null);
  // One store, shared with Helpwo — but bought from whichever product the user
  // happens to be looking at, because "go to the other site" is not an answer.
  const [storage, setStorage] = useState(null);
  const [buying, setBuying] = useState('');
  const [buyError, setBuyError] = useState(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (!session) return;
    fetch('/api/balance', { credentials: 'include' })
      .then(r => r.json())
      .then(d => {
        if (d.balance != null) setBalance(d.balance);
        setStorage(d.storage ?? null);
      })
      .catch(() => {});
  }, [session, reload]);

  useEffect(() => {
    if (!session) return;
    fetch('/api/subscription', { credentials: 'include' })
      .then(r => r.json())
      .then(d => setCalls((d?.byProduct || []).find(p => p.productId === 'cli')?.monthly ?? null))
      .catch(() => {});
  }, [session, reload]);

  async function buyPack(kind) {
    setBuying(kind);
    setBuyError(null);
    try {
      const res = await fetch('/api/subscription/upgrades', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind }),
      });
      const body = await res.json().catch(() => ({}));
      // The server's own words: it knows whether the problem was a missing
      // membership or an empty balance, and those need different answers.
      if (!res.ok) setBuyError({ kind, message: body.detail || body.error || '加购失败，请重试。' });
      else setReload(v => v + 1);
    } catch {
      setBuyError({ kind, message: '加购失败，请重试。' });
    } finally {
      setBuying('');
    }
  }

  // Whole megabytes turn a real 84 KB of files into "0 MB", which reads as a
  // broken counter rather than as a nearly-empty store.
  function fmtBytes(n) {
    if (!Number.isFinite(n)) return '—';
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
    if (n >= 10 * 1048576) return `${Math.round(n / 1048576)} MB`;
    if (n >= 1048576) return `${(n / 1048576).toFixed(1).replace(/\.0$/, '')} MB`;
    if (n >= 1024) return `${Math.round(n / 1024)} KB`;
    return `${n} B`;
  }

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await auth.signOut();
    } finally {
      window.location.replace('https://accounts.laintas.com/login');
    }
  }

  const inputClass = "w-full px-4 py-2.5 rounded-xl text-sm outline-none transition-all duration-200";

  if (isPending) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="w-5 h-5 border-2 border-current border-t-transparent rounded-full animate-spin" style={{ color: 'var(--text-muted)' }} />
      </div>
    );
  }

  if (!session) {
    return (
      <div className="min-h-screen flex items-center justify-center px-4 pt-20">
        <div className="text-center rounded-2xl p-8 max-w-md" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
          <h2 className="font-display text-xl font-semibold italic mb-3" style={{ color: 'var(--text-primary)' }}>未登录</h2>
          <p className="text-sm mb-6" style={{ color: 'var(--text-tertiary)' }}>请先登录后访问设置页面</p>
          <a href="/api/sso/login?return_to=%2Fsettings" className="inline-block px-6 py-2.5 rounded-xl font-semibold text-sm transition-all"
            style={{ background: 'var(--accent-soft)', color: 'var(--text-primary)' }}>登录</a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen px-4 pt-28 pb-16">
      <div className="max-w-2xl mx-auto">
        <motion.div initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0.25, 0.46, 0.45, 0.94] }}>
          <Link to="/" className="inline-flex items-center gap-2 text-sm transition-colors mb-8 font-mono"
            style={{ color: 'var(--text-muted)' }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
            返回首页
          </Link>

          {/* Profile */}
          <div className="rounded-2xl p-6 mb-5" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
            <h2 className="font-display text-lg italic font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>个人资料</h2>
            <div className="flex items-center gap-4 mb-6">
              <div className="w-14 h-14 rounded-full flex items-center justify-center font-display text-xl italic font-bold"
                style={{ background: 'var(--accent-dim)', color: 'var(--accent-soft)', border: '1px solid var(--border-accent)' }}>
                {session.user?.name?.charAt(0)?.toUpperCase() || session.user?.email?.charAt(0)?.toUpperCase() || '?'}
              </div>
              <div>
                <p className="font-medium" style={{ color: 'var(--text-primary)' }}>{session.user?.name || 'Guest'}</p>
                <p className="text-sm font-mono" style={{ color: 'var(--text-muted)' }}>{session.user?.email}</p>
              </div>
            </div>
            <div className="flex items-center justify-between py-2.5 px-4 rounded-xl" style={{ background: 'var(--input-bg)' }}>
              <span className="text-sm" style={{ color: 'var(--text-tertiary)' }}>用户 ID</span>
              <span className="font-mono text-sm" style={{ color: 'var(--text-secondary)' }}>{session.user?.id?.slice(0, 12) || '—'}</span>
            </div>
          </div>

          {/* Balance */}
          <div className="rounded-2xl p-6 mb-5" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
            <h2 className="font-display text-lg italic font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>账户余额</h2>
            <div className="p-4 rounded-xl" style={{ background: 'var(--accent-dim)', border: '1px solid var(--border-accent)' }}>
              <div className="flex items-center justify-between">
                <span className="text-sm" style={{ color: 'var(--text-tertiary)' }}>当前余额</span>
                <span className="font-display text-2xl font-semibold italic" style={{ color: 'var(--accent-soft)' }}>
                  ${balance != null ? (balance / 100).toFixed(2) : '0.00'}
                </span>
              </div>
            </div>
          </div>

          {/* CLI call allowance — the bar, what it is made of, and the one
              thing that makes it bigger. */}
          {calls && calls.limit != null && (
            <div className="rounded-2xl p-6 mb-5" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
              <h2 className="font-display text-lg italic font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>调用额度</h2>
              <div className="p-4 rounded-xl" style={{ background: 'var(--input-bg)' }}>
                <div className="flex items-baseline justify-between mb-2">
                  <span className="text-sm" style={{ color: 'var(--text-tertiary)' }}>本月 CLI 调用</span>
                  <span className="font-mono text-sm" style={{ color: 'var(--text-primary)' }}>
                    {calls.used.toLocaleString()} / {calls.limit.toLocaleString()}
                  </span>
                </div>
                <div className="h-1.5 rounded-full overflow-hidden mb-2" style={{ background: 'var(--bg-hover)' }}>
                  <div className="h-full rounded-full transition-all" style={{
                    width: `${Math.min(100, (calls.used / calls.limit) * 100)}%`,
                    background: calls.used >= calls.limit ? 'var(--error, #ef4444)'
                      : calls.used / calls.limit >= 0.9 ? 'var(--warning, #d97706)'
                        : 'var(--accent-soft)',
                  }} />
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    {calls.purchased > 0
                      ? `含 ${(calls.included ?? 0).toLocaleString()} + 已加购 ${calls.purchased.toLocaleString()}`
                      : '额度用尽后按 Token 计费扣余额'}
                  </span>
                  {calls.top_up && (
                    <button onClick={() => buyPack(calls.top_up.kind)} disabled={!!buying}
                      className="flex-none text-xs px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                      style={{ background: 'var(--bg-hover)', color: 'var(--text-secondary)' }}>
                      {buying === calls.top_up.kind ? '处理中…'
                        : `加购 ${calls.top_up.quantity.toLocaleString()} 次 · $${(calls.top_up.monthly_cents / 100).toFixed(0)}/月`}
                    </button>
                  )}
                </div>
              </div>
              {buyError?.kind === calls.top_up?.kind && (
                <p className="text-xs mt-3" style={{ color: 'var(--error, #ef4444)' }}>{buyError.message}</p>
              )}
              <p className="text-xs mt-3 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                增值项加在会员上，与会员同日扣费，不会多出第二个账期。可随时取消，已付的当月照常供应。
              </p>
            </div>
          )}

          {/* Shared storage — what `/shared` and Helpwo's "Laintas 存储" both
              write into. Sold here as well as in Helpwo: it is one allowance,
              and the person who filled it from the CLI is standing here. */}
          {storage && storage.limit_bytes != null && (
            <div className="rounded-2xl p-6 mb-5" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
              <h2 className="font-display text-lg italic font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>存储空间</h2>
              <div className="p-4 rounded-xl" style={{ background: 'var(--input-bg)' }}>
                <div className="flex items-baseline justify-between mb-2">
                  <span className="text-sm" style={{ color: 'var(--text-tertiary)' }}>已用空间</span>
                  <span className="font-mono text-sm" style={{ color: 'var(--text-primary)' }}>
                    {fmtBytes(storage.used_bytes)} / {fmtBytes(storage.limit_bytes)}
                  </span>
                </div>
                <div className="h-1.5 rounded-full overflow-hidden mb-2" style={{ background: 'var(--bg-hover)' }}>
                  <div className="h-full rounded-full transition-all" style={{
                    width: `${Math.min(100, (storage.used_bytes / storage.limit_bytes) * 100)}%`,
                    background: storage.read_only ? 'var(--error, #ef4444)'
                      : storage.used_bytes / storage.limit_bytes >= 0.9 ? 'var(--warning, #d97706)'
                        : 'var(--accent-soft)',
                  }} />
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    {storage.extra_bytes > 0
                      ? `含 ${fmtBytes(storage.base_bytes)} + 已加购 ${fmtBytes(storage.extra_bytes)}`
                      : '与 Helpwo 共用同一份存储'}
                  </span>
                  {storage.top_up && (
                    <button onClick={() => buyPack(storage.top_up.kind)} disabled={!!buying}
                      className="flex-none text-xs px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                      style={{ background: 'var(--bg-hover)', color: 'var(--text-secondary)' }}>
                      {buying === storage.top_up.kind ? '处理中…'
                        : `加购 ${fmtBytes(storage.top_up.quantity)} · $${(storage.top_up.monthly_cents / 100).toFixed(0)}/月`}
                    </button>
                  )}
                </div>
              </div>
              {buyError?.kind === storage.top_up?.kind && (
                <p className="text-xs mt-3" style={{ color: 'var(--error, #ef4444)' }}>{buyError.message}</p>
              )}
              <p className="text-xs mt-3 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                {storage.read_only
                  ? '空间已满：现有文件不会被删除，但暂时无法写入新文件，加购后立即恢复。'
                  : '空间满了只会停止写入，不会删除已有文件，也不会额外扣费。'}
              </p>
            </div>
          )}

          {/* Preferences */}
          <div className="rounded-2xl p-6 mb-5" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
            <h2 className="font-display text-lg italic font-semibold mb-4" style={{ color: 'var(--text-primary)' }}>偏好设置</h2>
            <div className="space-y-2">
              <div className="flex items-center justify-between py-2.5 px-4 rounded-xl" style={{ background: 'var(--input-bg)' }}>
                <span className="text-sm" style={{ color: 'var(--text-tertiary)' }}>主题模式</span>
                <button onClick={toggleTheme} className="flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg transition-colors"
                  style={{ background: 'var(--bg-hover)', color: 'var(--text-secondary)' }}>
                  {theme === 'dark' ? (
                    <><svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M13.5 10.5A6 6 0 011.5 6.5 5 5 0 0013.5 10.5z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/></svg> 暗色</>
                  ) : (
                    <><svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.5"/><path d="M8 1v1.5M8 13.5V15M15 8h-1.5M2.5 8H1M12.95 3.05l-1.06 1.06M4.11 11.89l-1.06 1.06M12.95 12.95l-1.06-1.06M4.11 4.11L3.05 3.05" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/></svg> 亮色</>
                  )}
                </button>
              </div>
              <div className="flex items-center justify-between py-2.5 px-4 rounded-xl" style={{ background: 'var(--input-bg)' }}>
                <span className="text-sm" style={{ color: 'var(--text-tertiary)' }}>界面语言</span>
                <button onClick={toggleLang} className="flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg transition-colors"
                  style={{ background: 'var(--bg-hover)', color: 'var(--text-secondary)' }}>
                  {lang === 'zh' ? '中文' : 'English'}
                </button>
              </div>
            </div>
          </div>

          {/* Danger Zone */}
          <div className="rounded-2xl p-6" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}>
            <h2 className="font-display text-lg italic font-semibold mb-4" style={{ color: 'var(--danger)' }}>危险操作</h2>
            <button onClick={handleSignOut} disabled={signingOut}
              className="w-full py-2.5 rounded-xl font-medium text-sm disabled:opacity-50 transition-all"
              style={{ background: 'var(--danger-bg)', color: 'var(--danger)', border: '1px solid var(--danger-border)' }}>
              {signingOut ? (
                <span className="inline-flex items-center gap-2"><span className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />正在退出...</span>
              ) : '退出登录'}
            </button>
          </div>
        </motion.div>
      </div>
    </div>
  );
}
