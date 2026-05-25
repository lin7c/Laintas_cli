import { useState, useRef, useEffect } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import { useAuth } from '../contexts/AuthContext';
import { useLanguage } from '../contexts/LanguageContext';

export default function LoginPage() {
  const { t } = useLanguage();
  const auth = useAuth();
  const { data: session, isPending: sessionLoading } = auth.useSession();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const redirect = searchParams.get('redirect');

  const [form, setForm] = useState({ email: '', password: '', remember: false });
  const [showPassword, setShowPassword] = useState(false);
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const usernameRef = useRef(null);

  useEffect(() => { usernameRef.current?.focus(); }, []);

  useEffect(() => {
    if (!sessionLoading && session) {
      navigate(redirect || '/settings', { replace: true });
    }
  }, [session, sessionLoading, navigate, redirect]);

  function validate() {
    const errs = {};
    if (!form.email.trim()) errs.email = '请输入邮箱或用户名';
    if (!form.password) errs.password = '请输入密码';
    return errs;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setServerError('');
    const errs = validate();
    setErrors(errs);
    if (Object.keys(errs).length) return;

    setSubmitting(true);
    try {
      const identifier = form.email.trim();
      const isEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(identifier);
      const result = isEmail
        ? await auth.signIn.email({ email: identifier, password: form.password, rememberMe: form.remember })
        : await auth.signIn.username({ username: identifier, password: form.password, rememberMe: form.remember });

      if (result?.error) {
        setServerError(result.error.message || '登录失败，请重试');
        setSubmitting(false);
        return;
      }
      navigate(redirect || '/settings', { replace: true });
    } catch (err) {
      setServerError(err?.message || '登录失败，请重试');
      setSubmitting(false);
    }
  }

  function update(field, value) {
    setForm(f => ({ ...f, [field]: value }));
    if (errors[field]) setErrors(e => ({ ...e, [field]: '' }));
    if (serverError) setServerError('');
  }

  if (sessionLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="w-5 h-5 border-2 border-current border-t-transparent rounded-full animate-spin" style={{ color: 'var(--text-muted)' }} />
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4 pt-20">
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.25, 0.46, 0.45, 0.94] }}
        className="w-full max-w-md"
      >
        <div className="rounded-2xl p-8" style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)', backdropFilter: 'blur(24px)' }}>
          <div className="text-center mb-8">
            <div className="inline-flex items-center justify-center w-10 h-10 rounded-full mb-5"
              style={{ background: 'var(--accent-dim)', border: '1px solid var(--border-accent)' }}>
              <span className="font-display text-lg italic font-bold" style={{ color: 'var(--accent-soft)' }}>L</span>
            </div>
            <h1 className="font-display text-2xl font-semibold italic mb-1.5" style={{ color: 'var(--text-primary)' }}>
              登录 Laintas
            </h1>
            <p className="text-sm" style={{ color: 'var(--text-tertiary)' }}>欢迎回来</p>
          </div>

          {serverError && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}
              className="mb-6 p-3.5 rounded-xl text-sm text-center"
              style={{ background: 'var(--danger-bg)', border: '1px solid var(--danger-border)', color: 'var(--danger)' }}>
              {serverError}
            </motion.div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--text-secondary)' }}>邮箱或用户名</label>
              <input ref={usernameRef} type="text" value={form.email}
                onChange={e => update('email', e.target.value)}
                placeholder="email@example.com"
                autoComplete="username"
                style={{ background: 'var(--input-bg)', border: errors.email ? '1px solid var(--danger)' : '1px solid var(--input-border)', color: 'var(--text-primary)' }}
                className="w-full px-4 py-2.5 rounded-xl text-sm outline-none transition-all duration-200 placeholder:text-[var(--text-muted)] focus:border-[var(--accent-soft)]" />
              {errors.email && <p className="mt-1 text-xs" style={{ color: 'var(--danger)' }}>{errors.email}</p>}
            </div>
            <div>
              <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--text-secondary)' }}>密码</label>
              <div className="relative">
                <input type={showPassword ? 'text' : 'password'} value={form.password}
                  onChange={e => update('password', e.target.value)}
                  placeholder="••••••••" autoComplete="current-password"
                  style={{ background: 'var(--input-bg)', border: errors.password ? '1px solid var(--danger)' : '1px solid var(--input-border)', color: 'var(--text-primary)' }}
                  className="w-full px-4 py-2.5 pr-10 rounded-xl text-sm outline-none transition-all duration-200 placeholder:text-[var(--text-muted)] focus:border-[var(--accent-soft)]" />
                <button type="button" onClick={() => setShowPassword(s => !s)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors" style={{ color: 'var(--text-muted)' }}>
                  {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                </button>
              </div>
              {errors.password && <p className="mt-1 text-xs" style={{ color: 'var(--danger)' }}>{errors.password}</p>}
            </div>
            <button type="submit" disabled={submitting}
              className="w-full py-3 rounded-xl font-semibold text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-all active:scale-[0.98] cursor-pointer"
              style={{ background: 'var(--accent-soft)', color: 'var(--bg-root)', boxShadow: '0 2px 12px var(--accent-glow)' }}>
              {submitting ? (
                <span className="inline-flex items-center gap-2"><span className="w-4 h-4 border-2 border-current/20 border-t-current rounded-full animate-spin" />登录中...</span>
              ) : '登录'}
            </button>
          </form>

          <div className="mt-6">
            <div className="relative mb-5">
              <div className="absolute inset-0 flex items-center"><div className="w-full border-t" style={{ borderColor: 'var(--border-subtle)' }} /></div>
              <div className="relative flex justify-center text-xs">
                <span className="px-3 py-0.5 rounded-full font-mono text-[0.65rem] tracking-wider" style={{ background: 'var(--bg-surface)', color: 'var(--text-muted)' }}>或通过第三方登录</span>
              </div>
            </div>
            <div className="flex gap-3">
              <button type="button" onClick={async () => {
                await auth.signIn.social({ provider: 'google', callbackURL: redirect ? `${window.location.origin}/login?redirect=${encodeURIComponent(redirect)}` : '/settings' });
              }}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium transition-all active:scale-[0.98] cursor-pointer"
              style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-strong)', color: 'var(--text-primary)' }}>
                <GoogleIcon /> Google
              </button>
              <button type="button" onClick={async () => {
                await auth.signIn.social({ provider: 'github', callbackURL: redirect ? `${window.location.origin}/login?redirect=${encodeURIComponent(redirect)}` : '/settings' });
              }}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium transition-all active:scale-[0.98] cursor-pointer"
              style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-strong)', color: 'var(--text-primary)' }}>
                <GitHubIcon /> GitHub
              </button>
            </div>
          </div>

          <p className="mt-6 text-center text-sm" style={{ color: 'var(--text-tertiary)' }}>
            还没有账号？{' '}
            <Link to={`/register${redirect ? `?redirect=${encodeURIComponent(redirect)}` : ''}`} className="font-medium transition-colors" style={{ color: 'var(--accent-soft)' }}>注册</Link>
          </p>
        </div>
      </motion.div>
    </div>
  );
}

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" />
    </svg>
  );
}
function EyeOffIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24" />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  );
}
function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
    </svg>
  );
}
function GitHubIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path fillRule="evenodd" clipRule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" />
    </svg>
  );
}
