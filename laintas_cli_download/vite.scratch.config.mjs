import base from './vite.config.js';
export default {
  ...base,
  publicDir: false,
  build: { ...(base.build || {}), outDir: 'build-scratch', emptyOutDir: true },
};
