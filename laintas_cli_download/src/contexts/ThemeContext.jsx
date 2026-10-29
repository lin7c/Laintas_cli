import { readUiPreference, saveUiPreference } from '../../../../laintas/shared/ui-preferences.js';
import { createContext, useContext, useState, useEffect } from 'react';

const ThemeContext = createContext(null);

export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState(() => {
    try { return readUiPreference('theme'); }
    catch { return 'dark'; }
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('laintas-theme', theme); } catch {}
  }, [theme]);

  const toggleTheme = () => setTheme(t => { const next=t === 'dark' ? 'light' : 'dark'; saveUiPreference('theme',next); return next; });

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider');
  return ctx;
}
